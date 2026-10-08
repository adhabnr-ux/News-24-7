"""Sending through the macOS Messages app with AppleScript (used by the relay agent and by the
``imessage`` channel when News247 itself runs on a Mac)."""

from __future__ import annotations

import asyncio
import platform

# Arguments are passed via argv, never interpolated into the script, so a headline can't inject
# AppleScript. AppleScript compiles a whole script up front and Apple renamed the dictionary terms
# over the years, so each syntax is a separate script, tried in order:
#   1. participant-of-account  (macOS 11 Big Sur and later, including macOS 26)
#   2. buddy-of-service        (macOS 10.x)
#   3. an existing 1:1 chat id (last resort)
_HEAD = """
on run argv
    set targetHandle to item 1 of argv
    set messageText to item 2 of argv
    set wantSMS to (item 3 of argv is "sms")
    tell application "Messages"
"""
_TAIL = """
    end tell
end run
"""
IMESSAGE_SCRIPTS = [
    _HEAD
    + """
        if wantSMS then
            set targetAccount to 1st account whose service type = SMS
        else
            set targetAccount to 1st account whose service type = iMessage
        end if
        send messageText to participant targetHandle of targetAccount
"""
    + _TAIL,
    _HEAD
    + """
        if wantSMS then
            set targetService to 1st service whose service type = SMS
        else
            set targetService to 1st service whose service type = iMessage
        end if
        send messageText to buddy targetHandle of targetService
"""
    + _TAIL,
    _HEAD
    + """
        if wantSMS then
            send messageText to chat id ("SMS;-;" & targetHandle)
        else
            send messageText to chat id ("iMessage;-;" & targetHandle)
        end if
"""
    + _TAIL,
]

# Lists the Messages accounts ("iMessage|true", "SMS|false" ...) for `news247 relay --doctor`.
ACCOUNTS_SCRIPT = """
tell application "Messages"
    set out to ""
    repeat with a in accounts
        try
            set out to out & (service type of a as text) & "|" & (enabled of a as text) & linefeed
        on error
            set out to out & (service type of a as text) & "|?" & linefeed
        end try
    end repeat
    return out
end tell
"""

AUTOMATION_HINT = "allow Automation access: System Settings > Privacy & Security > Automation"


class MessagesApp:
    """Sends iMessages (or SMS via iPhone forwarding) from this Mac's Messages app."""

    def __init__(self, timeout_s: float = 30.0) -> None:
        self.timeout_s = timeout_s
        self._working_script = 0  # remember which syntax works on this Mac

    @staticmethod
    def supported() -> bool:
        return platform.system() == "Darwin"

    async def osascript(self, script: str, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            script,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(
                f"Messages did not respond within {self.timeout_s:.0f}s (is it running and signed in?)"
            ) from None
        if proc.returncode != 0:
            raise RuntimeError(err.decode(errors="replace").strip() or f"osascript exited {proc.returncode}")
        return (out or b"").decode(errors="replace")

    def _script_order(self) -> list[int]:
        first = self._working_script
        return [first] + [i for i in range(len(IMESSAGE_SCRIPTS)) if i != first]

    async def send(self, handle: str, text: str, service: str = "imessage") -> None:
        """Send one message. ``service``: "imessage" or "sms". Raises RuntimeError on failure."""
        if not self.supported():
            raise RuntimeError(
                "imessage needs macOS with Messages signed in (use the relay or another channel elsewhere)"
            )
        errors = []
        for i in self._script_order():
            try:
                await self.osascript(IMESSAGE_SCRIPTS[i], handle, text, service)
                self._working_script = i
                return
            except RuntimeError as exc:
                msg = str(exc)
                if "-1743" in msg or "Not authorized" in msg:
                    raise RuntimeError(f"{msg} — {AUTOMATION_HINT}") from None
                errors.append(msg)
        raise RuntimeError("Messages could not send: " + " | ".join(errors))

    async def accounts(self) -> list[tuple[str, str]]:
        """[(service type, enabled)] for every account configured in Messages."""
        out = await self.osascript(ACCOUNTS_SCRIPT)
        rows = []
        for line in out.splitlines():
            if "|" in line:
                svc, enabled = line.split("|", 1)
                rows.append((svc.strip(), enabled.strip()))
        return rows
