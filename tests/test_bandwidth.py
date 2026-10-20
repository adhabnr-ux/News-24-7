"""Free hosts meter bandwidth: text responses are gzipped, repeat GETs of unchanged content cost a
304, and streams, downloads and writes are left alone."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import aiohttp
from aiohttp.test_utils import TestServer

from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine

H = {"Authorization": "Bearer tok", "X-Device": DEV}


async def test_text_is_gzipped_and_unchanged_content_costs_a_304(tmp_path):
    eng = semis_engine(tmp_path)
    for i in range(60):
        a = news(f"Acme headline number {i} with a reasonably long title about contracts")
        a.id = f"{i:016x}"
        eng.storage.add_alert(a)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    try:
        async with aiohttp.ClientSession(auto_decompress=False) as s:
            async with s.get(srv.make_url("/api/app"), headers={**H, "Accept-Encoding": "gzip"}) as r:
                raw = await r.read()
                assert r.headers["Content-Encoding"] == "gzip" and r.headers["Vary"] == "Accept-Encoding"
                assert r.headers["Cache-Control"] == "no-cache" and r.headers["ETag"].startswith('W/"')
                etag = r.headers["ETag"]
                plain = gzip.decompress(raw)
                assert (
                    len(raw) < len(plain) / 4 and json.loads(plain)["alerts"]
                )  # the 50 KB list shrinks a lot
            async with s.get(srv.make_url("/api/app"), headers={**H, "Accept-Encoding": "gzip"}) as r:
                again = r.headers.get("ETag")
            if again == etag:  # unchanged since: a bodiless 304
                async with s.get(srv.make_url("/api/app"), headers={**H, "If-None-Match": etag}) as r:
                    assert r.status == 304 and await r.read() == b"" and r.headers["ETag"] == etag
            async with s.get(srv.make_url("/api/app"), headers={**H, "If-None-Match": 'W/"stale"'}) as r:
                assert r.status == 200
            # no gzip unless asked for; small bodies aren't worth it
            async with s.get(srv.make_url("/api/app"), headers={**H, "Accept-Encoding": "identity"}) as r:
                assert (
                    "Content-Encoding" not in r.headers and abs(len(await r.read()) - len(plain)) < 50
                )  # the payload carries a clock
            async with s.get(srv.make_url("/health"), headers={"Accept-Encoding": "gzip"}) as r:
                assert "Content-Encoding" not in r.headers and r.headers["ETag"]
            # the app shell: gzipped, revalidated by ETag, long-cached files left alone
            async with s.get(srv.make_url("/app/app.js"), headers={"Accept-Encoding": "gzip"}) as r:
                js = await r.read()
                # gzipped to under a third of the file (the shell is cached per version, and a repeat
                # load is a ~200-byte 304), and well inside a free host's budget either way
                raw_js = (Path(__file__).resolve().parent.parent / "news247/web/static/app/app.js").stat().st_size
                assert r.headers["Content-Encoding"] == "gzip" and len(js) < raw_js / 3 and len(js) < 150_000
                etag = r.headers["ETag"]
            async with s.get(srv.make_url("/app/app.js"), headers={"If-None-Match": etag}) as r:
                assert r.status == 304
            async with s.get(srv.make_url("/app/launch.webp"), headers={"Accept-Encoding": "gzip"}) as r:
                assert "Content-Encoding" not in r.headers and "immutable" in r.headers["Cache-Control"]
            # writes and the live stream are untouched
            async with s.post(
                srv.make_url("/api/desk"),
                json={"action": "watch_add", "symbol": "ACME"},
                headers={**H, "Accept-Encoding": "gzip"},
            ) as r:
                assert r.status == 200 and "ETag" not in r.headers
            async with s.get(srv.make_url("/events"), headers={**H, "Accept-Encoding": "gzip"}) as r:
                assert (
                    r.headers["Content-Type"] == "text/event-stream" and "Content-Encoding" not in r.headers
                )
                assert "ETag" not in r.headers
    finally:
        await srv.close()
        await eng.http.close()
