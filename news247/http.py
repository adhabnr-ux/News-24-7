"""Shared async HTTP client with conditional GET, per-host politeness and retry-after."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import aiohttp

log = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/rss+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class HTTPError(Exception):
    def __init__(self, status: int, url: str, retry_after: float | None = None, body: str = "") -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url
        self.retry_after = retry_after
        self.body = body


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str]
    url: str
    elapsed: float
    not_modified: bool = False

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        import json

        return json.loads(self.body)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


class HttpClient:
    """Wraps one aiohttp session for the whole process.

    * Conditional GET: remembers ETag / Last-Modified per URL so frequent polls of an
      unchanged feed cost a 304 and no parsing.
    * Per-host minimum spacing (e.g. SEC asks for <= 10 req/s) and a global concurrency cap.
    """

    def __init__(
        self,
        user_agent: str,
        timeout_s: float = 15.0,
        max_concurrency: int = 32,
        host_min_interval: dict[str, float] | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = aiohttp.ClientTimeout(total=timeout_s, sock_connect=min(10.0, timeout_s))
        self._sem = asyncio.Semaphore(max_concurrency)
        self._validators: dict[str, tuple[str | None, str | None]] = {}
        self._host_min_interval = {"www.sec.gov": 0.15, "efts.sec.gov": 0.15, **(host_min_interval or {})}
        self._host_next: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> HttpClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None:
            connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                connector=connector,
                trust_env=True,  # honour HTTPS_PROXY etc.
                headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"},
            )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("HttpClient not started")
        return self._session

    async def _respect_host_spacing(self, host: str) -> None:
        gap = self._host_min_interval.get(host)
        if not gap:
            return
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            wait = self._host_next.get(host, 0.0) - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._host_next[host] = max(now, self._host_next.get(host, 0.0)) + gap

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        conditional: bool = False,
        timeout_s: float | None = None,
    ) -> Response:
        hdrs = dict(headers or {})
        if conditional:
            etag, last_mod = self._validators.get(url, (None, None))
            if etag:
                hdrs["If-None-Match"] = etag
            if last_mod:
                hdrs["If-Modified-Since"] = last_mod
        host = urlsplit(url).hostname or ""
        await self._respect_host_spacing(host)
        timeout = aiohttp.ClientTimeout(total=timeout_s) if timeout_s else None
        async with self._sem:
            t0 = time.monotonic()
            async with self.session.request(
                method, url, headers=hdrs, params=params, json=json, data=data, timeout=timeout
            ) as resp:
                body = await resp.read()
                elapsed = time.monotonic() - t0
                rheaders = {k: v for k, v in resp.headers.items()}
                if resp.status == 304:
                    return Response(304, b"", rheaders, str(resp.url), elapsed, not_modified=True)
                if resp.status >= 400:
                    raise HTTPError(
                        resp.status,
                        url,
                        _parse_retry_after(resp.headers.get("Retry-After")),
                        body[:500].decode("utf-8", "replace"),
                    )
                if conditional and method == "GET":
                    et, lm = resp.headers.get("ETag"), resp.headers.get("Last-Modified")
                    if et or lm:
                        self._validators[url] = (et, lm)
                return Response(resp.status, body, rheaders, str(resp.url), elapsed)

    async def get(self, url: str, **kw: Any) -> Response:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw: Any) -> Response:
        return await self.request("POST", url, **kw)

    def forget_validators(self, url: str) -> None:
        self._validators.pop(url, None)


def ws_receive_timeout(seconds: float) -> dict[str, object]:
    """``ws_connect`` kwargs for a receive timeout across aiohttp versions."""
    if hasattr(aiohttp, "ClientWSTimeout"):
        return {"timeout": aiohttp.ClientWSTimeout(ws_receive=seconds)}
    return {"receive_timeout": seconds}
