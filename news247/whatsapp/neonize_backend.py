"""The real WhatsApp engine: whatsmeow (Go) through the ``neonize`` Python binding.

Installed with ``pip install 'news247[whatsapp]'`` (the Docker image includes it). Everything
WhatsApp-specific lives here; the session logic in ``session.py`` never imports neonize.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

from .session import Events

log = logging.getLogger(__name__)


def available() -> tuple[bool, str]:
    """(True, "") if the engine can be imported, else (False, why)."""
    try:
        import neonize.aioze.client  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - ImportError, missing libmagic, wrong platform...
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def qr_svg(data: str) -> str:
    """Inline SVG of a pairing QR code (segno ships with neonize)."""
    try:
        import segno
    except ImportError:
        return ""
    return segno.make_qr(data, error="m").svg_inline(scale=5, border=2, dark="#000", light="#fff")


class NeonizeBackend:
    def __init__(self, db_path: Path, device_name: str = "News247") -> None:
        self.db_path = db_path
        self.device_name = device_name
        self.client: Any = None

    async def start(self, ev: Events) -> Awaitable[None]:
        from neonize.aioze.client import NewAClient
        from neonize.aioze.events import (
            ConnectedEv,
            ConnectFailureEv,
            DisconnectedEv,
            LoggedOutEv,
            MessageEv,
            PairStatusEv,
            ReceiptEv,
            TemporaryBanEv,
        )
        from neonize.proto import Neonize_pb2
        from neonize.proto.waCompanionReg.WAWebProtobufsCompanionReg_pb2 import DeviceProps

        with contextlib.suppress(Exception):  # media helpers are unused: skip the ffmpeg warning
            import neonize.utils.ffmpeg as _ff

            _ff._ffmpeg_checked = True
        logging.getLogger("whatsmeow").setLevel(logging.WARNING)

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        props = DeviceProps(os=self.device_name, platformType=DeviceProps.CHROME)
        client = NewAClient(str(self.db_path), props=props)
        self.client = client
        receipt_names = {
            v.number: v.name.lower()
            for v in Neonize_pb2.Receipt.DESCRIPTOR.fields_by_name["Type"].enum_type.values
        }

        async def on_qr(_: Any, data: bytes) -> None:
            ev.qr([data.decode(errors="replace")])

        async def on_connected(c: Any, _: Any) -> None:
            me, name = "", ""
            with contextlib.suppress(Exception):
                device = await c.get_me()
                me, name = device.JID.User, device.PushName
            ev.connected(me, name)

        async def on_pair(_: Any, p: Any) -> None:
            if p.Status == Neonize_pb2.PairStatus.SUCCESS:
                ev.paired(p.ID.User, p.BusinessName)
            else:
                ev.pair_failed(p.Error or "unknown error")

        async def on_logged_out(_: Any, e: Any) -> None:
            ev.logged_out(str(e.Reason))

        async def on_disconnected(_: Any, e: Any) -> None:
            ev.disconnected("")

        async def on_failure(_: Any, e: Any) -> None:
            ev.disconnected(f"connect failure {e.Reason}: {e.Message}".strip())

        async def on_ban(_: Any, e: Any) -> None:
            ev.banned(f"temporary ban (code {e.Code}), expires in {e.Expire}s")

        async def on_receipt(_: Any, r: Any) -> None:
            ev.receipt(
                list(r.MessageIDs), receipt_names.get(r.Type, str(r.Type)), r.MessageSource.Sender.User
            )

        async def on_message(c: Any, m: Any) -> None:
            src = m.Info.MessageSource
            if src.IsGroup:
                return
            sender = src.Sender
            if sender.Server == "lid":  # privacy ids: map back to a phone number
                if src.SenderAlt.User and src.SenderAlt.Server == "s.whatsapp.net":
                    sender = src.SenderAlt
                else:
                    with contextlib.suppress(Exception):
                        sender = await c.get_pn_from_lid(sender)
            msg = m.Message
            text = msg.conversation or msg.extendedTextMessage.text
            ev.message(sender.User, text, m.Info.ID, bool(src.IsFromMe))

        client.event.qr(on_qr)
        for event_type, handler in (
            (ConnectedEv, on_connected),
            (PairStatusEv, on_pair),
            (LoggedOutEv, on_logged_out),
            (DisconnectedEv, on_disconnected),
            (ConnectFailureEv, on_failure),
            (TemporaryBanEv, on_ban),
            (ReceiptEv, on_receipt),
            (MessageEv, on_message),
        ):
            client.event(event_type)(handler)
        return await client.connect()

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.stop()

    async def pair_code(self, phone: str) -> str:
        from neonize.utils.enum import ClientName

        return await self.client.PairPhone(phone, True, ClientName.LINUX)  # shown as "Chrome (Linux)"

    async def send_text(self, to: str, text: str) -> str:
        from neonize.utils.jid import build_jid

        resp = await self.client.send_message(build_jid(to), text)
        return str(resp.ID)

    async def logout(self) -> None:
        await asyncio.wait_for(self.client.logout(), 15)
