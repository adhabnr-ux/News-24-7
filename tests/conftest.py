from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from news247.config import Config, build_config
from news247.http import HttpClient
from news247.models import Alert
from news247.notify.channels import Notifier

FIXTURES = Path(__file__).parent / "fixtures"


def rfc822(ts: float) -> str:
    return format_datetime(datetime.fromtimestamp(ts, tz=timezone.utc), usegmt=True)


def fixture_text(name: str, **subs: str) -> str:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    for k, v in subs.items():
        text = text.replace("{" + k + "}", v)
    return text


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = build_config({"general": {"data_dir": str(tmp_path)}, "notify": {"console": {"enabled": False}}})
    return c


class Recorder:
    """A tiny programmable HTTP server that records every request."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.routes: dict[str, Any] = {}
        self.server: TestServer | None = None

    def on(self, path: str, response: Any) -> None:
        """response: (status, body[, headers]) tuple, dict/list (JSON) or callable(request)->web.Response."""
        self.routes[path] = response

    def url(self, path: str = "") -> str:
        assert self.server is not None
        return str(self.server.make_url(path))

    async def handler(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        rec = {
            "method": request.method,
            "path": request.path,
            "query": dict(request.query),
            "headers": dict(request.headers),
            "body": body,
        }
        try:
            rec["json"] = json.loads(body) if body else None
        except ValueError:
            rec["json"] = None
        self.requests.append(rec)
        resp = self.routes.get(request.path)
        if resp is None:
            return web.Response(status=404, text="not found")
        if callable(resp):
            out = resp(request)
            if hasattr(out, "__await__"):
                out = await out
            return out
        if isinstance(resp, (dict, list)):
            return web.json_response(resp)
        status, text, *rest = resp
        headers = rest[0] if rest else {}
        return web.Response(
            status=status, body=text.encode() if isinstance(text, str) else text, headers=headers
        )


@pytest.fixture
async def server() -> AsyncIterator[Recorder]:
    rec = Recorder()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", rec.handler)
    rec.server = TestServer(app)
    await rec.server.start_server()
    yield rec
    await rec.server.close()


@pytest.fixture
async def http() -> AsyncIterator[HttpClient]:
    async with HttpClient("News247Test/1.0 (test@example.com)", timeout_s=5) as client:
        yield client


class CaptureNotifier(Notifier):
    """In-memory channel used by engine tests."""

    name = "capture"

    def __init__(self, min_severity: Any = None) -> None:
        from news247.models import Severity

        self.options = {}
        self.http = None  # type: ignore[assignment]
        self.min_severity = min_severity or Severity.LOW
        self.sent = 0
        self.failed = 0
        self.last_error = ""
        self.alerts: list[Alert] = []

    async def send(self, alert: Alert) -> None:
        self.alerts.append(alert)


@pytest.fixture
def now() -> Callable[[], float]:
    return time.time
