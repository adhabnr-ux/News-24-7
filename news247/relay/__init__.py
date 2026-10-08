"""Self-hosted iMessage relay: the monitor (hub) hands alerts to a Mac (agent) that sends them
through the Messages app. See docs/IMESSAGE-RELAY.md."""

from .protocol import PROTOCOL_VERSION, WS_PATH

__all__ = ["PROTOCOL_VERSION", "WS_PATH"]
