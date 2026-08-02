"""Smoke test for the artifact tool — CJK titles must yield openable links.

str.isalnum() is true for CJK, so _safe_filename keeps a title like 测试报告
verbatim as the file stem. The regression this guards: the live URL was built
with the raw stem and handed to webbrowser.open(), which mangles non-ASCII to
?? (dead link; VS Code retried and gave up). The fix percent-encodes the stem
in every emitted URL, and the server decodes it back before matching the file.

Also guards audit finding #7: every server endpoint (/artifacts/*, /list,
/api/events) requires the per-session ?t=<nonce> — 401 without it — and the
SSE stream carries no Access-Control-Allow-Origin, so a hostile web page can
neither enumerate artifacts nor observe reload events cross-origin.

No real browser: webbrowser.open is monkeypatched to capture the URL.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).parent.parent))

from rockycode.engine import artifact as artifact_mod
from rockycode.engine.artifact import (
    ARTIFACT_DIR,
    ArtifactServer,
    _safe_filename,
    build_artifact_tools,
)

CJK_TITLE = "测试报告"


def _capture_opens() -> list[str]:
    opened: list[str] = []
    artifact_mod.webbrowser.open = lambda url, *a, **k: opened.append(url) or True
    return opened


def test_safe_filename():
    assert _safe_filename(CJK_TITLE) == CJK_TITLE, "CJK must survive sanitisation"
    assert _safe_filename("../evil") == ".._evil"  # traversal chars neutralised
    assert "%" not in _safe_filename("a%20b")  # '%'->'_': server unquote stays a no-op


async def test_live_cjk_roundtrip(opened: list[str]):
    import aiohttp

    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        server = ArtifactServer(workdir)
        await server.start()
        try:
            async def target():
                return server

            tools = build_artifact_tools(workdir=workdir, engine=SimpleNamespace(artifact_target=target))
            out = await tools["create_artifact"].fn(CJK_TITLE, "<p>你好，世界</p>")
            assert "[ok]" in out, out

            assert len(opened) == 1, f"expected one browser open, got {opened}"
            url = opened[0]
            assert url.isascii(), f"live URL must be percent-encoded ASCII: {url}"
            assert quote(CJK_TITLE) in url, url
            assert f"?t={server.token}" in url, f"emitted URL must carry the nonce: {url}"

            async with aiohttp.ClientSession() as session:
                # The encoded URL must serve the artifact, declared as UTF-8.
                async with session.get(url) as resp:
                    assert resp.status == 200, f"GET {url} -> {resp.status}"
                    ctype = resp.headers.get("Content-Type", "")
                    assert "text/html" in ctype and "charset=utf-8" in ctype.lower(), ctype
                    text = await resp.text()
                    assert CJK_TITLE in text and "你好，世界" in text
                    # The injected reload script must reach SSE with the nonce.
                    assert f"/api/events?t={server.token}" in text

                # /list must emit encoded (ASCII) URLs that themselves resolve.
                async with session.get(f"{server.base_url}/list?t={server.token}") as resp:
                    items = await resp.json()
                assert items and items[0]["name"] == CJK_TITLE, items
                assert items[0]["url"].isascii(), items[0]["url"]
                async with session.get(items[0]["url"]) as resp:
                    assert resp.status == 200

                # Traversal still rejected after decoding (auth passes first).
                bad = f"{server.base_url}/artifacts/{quote('../evil', safe='')}?t={server.token}"
                async with session.get(bad) as resp:
                    assert resp.status == 400, resp.status
        finally:
            await server.stop()


async def test_static_cjk_file_uri(opened: list[str]):
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        tools = build_artifact_tools(workdir=workdir, engine=None)  # no target -> static
        out = await tools["create_artifact"].fn(CJK_TITLE, "<p>静态</p>")
        assert "[ok]" in out, out

        assert len(opened) == 1, f"expected one browser open, got {opened}"
        uri = opened[0]
        assert uri.startswith("file://") and uri.isascii(), f"file URI must be encoded: {uri}"
        # The encoded URI must decode back to the real on-disk file.
        fpath = Path(unquote(urlsplit(uri).path))
        assert fpath.exists() and fpath.stem == CJK_TITLE, fpath
        assert (workdir / ARTIFACT_DIR / f"{CJK_TITLE}.html").exists()


async def test_token_gate(opened: list[str]):
    """Audit #7: no endpoint answers without the per-session nonce, and the
    SSE stream has no wildcard CORS (same-origin reload script needs none)."""
    import aiohttp

    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        server = ArtifactServer(workdir)
        await server.start()
        try:
            async def target():
                return server

            tools = build_artifact_tools(workdir=workdir, engine=SimpleNamespace(artifact_target=target))
            out = await tools["create_artifact"].fn("gate", "<p>gated</p>")
            assert "[ok]" in out, out

            async with aiohttp.ClientSession() as session:
                # Missing and wrong tokens -> 401 on every endpoint.
                for path in ("/artifacts/gate", "/list", "/api/events"):
                    async with session.get(f"{server.base_url}{path}") as resp:
                        assert resp.status == 401, f"{path} without token -> {resp.status}"
                    async with session.get(f"{server.base_url}{path}?t=wrong") as resp:
                        assert resp.status == 401, f"{path} with bad token -> {resp.status}"

                # With the token: SSE streams, and no wildcard CORS leaks out.
                async with session.get(f"{server.base_url}/api/events?t={server.token}") as resp:
                    assert resp.status == 200, resp.status
                    assert "Access-Control-Allow-Origin" not in resp.headers, dict(resp.headers)
                    line = await asyncio.wait_for(resp.content.readline(), timeout=5)
                    assert b"connected" in line, line
                    await server.broadcast("reload", {"name": "gate"})
                    for _ in range(6):  # skip data:/blank lines of the connected event
                        line = await asyncio.wait_for(resp.content.readline(), timeout=5)
                        if b"reload" in line:
                            break
                    else:
                        raise AssertionError("no reload event received over SSE")
            # Client is gone; one more broadcast makes the handler's next write
            # fail fast so stop() doesn't wait out the 30 s keepalive timeout.
            await server.broadcast("reload", {"name": "wake"})
        finally:
            await server.stop()


async def test_server_reuse_and_keepalive():
    """Repeated starts reuse one listener; SSE stays open across keepalives."""
    import aiohttp

    old_keepalive = artifact_mod.SSE_KEEPALIVE_S
    artifact_mod.SSE_KEEPALIVE_S = 0.05
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            server = ArtifactServer(Path(tmpdir))
            await server.start()
            runner, port = server._runner, server.port
            await server.start()
            assert server._runner is runner and server.port == port, \
                "start() must be idempotent — repeated artifacts reuse one listener"
            try:
                async with aiohttp.ClientSession() as session:
                    url = f"{server.base_url}/api/events?t={server.token}"
                    async with session.get(url) as resp:
                        assert b"connected" in await asyncio.wait_for(
                            resp.content.readline(), timeout=1)
                        # Wait through one keepalive, then prove the same stream
                        # still receives a later reload event.
                        for _ in range(8):
                            line = await asyncio.wait_for(resp.content.readline(), timeout=1)
                            if line.startswith(b": keepalive"):
                                break
                        else:
                            raise AssertionError("SSE keepalive was not emitted")
                        await server.broadcast("reload", {"name": "after-keepalive"})
                        for _ in range(8):
                            line = await asyncio.wait_for(resp.content.readline(), timeout=1)
                            if b"reload" in line:
                                break
                        else:
                            raise AssertionError("SSE stream closed after keepalive")
            finally:
                await server.stop()
            assert server._runner is None and server.port == 0
    finally:
        artifact_mod.SSE_KEEPALIVE_S = old_keepalive


async def test_session_registry_tracks_browser_clients():
    """One session inventories artifacts and marks connected browser tabs."""
    import aiohttp

    old_keepalive = artifact_mod.SSE_KEEPALIVE_S
    artifact_mod.SSE_KEEPALIVE_S = 0.05
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            engine = SimpleNamespace()
            tools = build_artifact_tools(workdir=workdir, engine=engine)
            registry = engine.artifact_registry
            actions = []
            registry.subscribe(lambda action, record: actions.append(
                (action, record.name, record.open_clients)))
            server = ArtifactServer(workdir, registry=registry)

            async def target():
                await server.start()
                return server

            engine.artifact_target = target
            out = await tools["create_artifact"].fn("Session report", "<p>tracked</p>")
            assert "[ok]" in out
            record = registry.get("Session_report")
            assert registry.count == 1 and record is not None
            assert record.live and record.open_clients == 0
            assert actions[0][:2] == ("created", "Session_report"), actions

            replacement = None
            try:
                url = (f"{server.base_url}/api/events?t={server.token}"
                       "&artifact=Session_report")
                async with aiohttp.ClientSession() as client:
                    async with client.get(url) as response:
                        assert b"connected" in await response.content.readline()
                        assert record.open_clients == 1
                await server.broadcast("reload", {"name": "Session_report"})
                for _ in range(20):
                    if record.open_clients == 0:
                        break
                    await asyncio.sleep(0.01)
                assert record.open_clients == 0, "closed browser connection stayed marked open"
                assert ("clients", "Session_report", 1) in actions
                assert actions[-1] == ("clients", "Session_report", 0), actions

                # A manual stop/restart may change both port and token. Older
                # session entries and their EventSource script must be rebound.
                await server.stop()
                replacement = ArtifactServer(workdir, registry=registry)
                await replacement.start()
                assert replacement.token in record.url, record.url
                saved = record.path.read_text()
                assert replacement.token in saved and replacement.base_url in saved
            finally:
                await server.stop()
                if replacement is not None:
                    await replacement.stop()
    finally:
        artifact_mod.SSE_KEEPALIVE_S = old_keepalive


async def test_serve_owns_artifact_lifecycle():
    """The JSON-RPC serve adapter retains and closes its session server."""
    from rockycode.engine import server as server_mod

    class FakeEngine:
        def __init__(self, *, model, workdir, **kwargs):
            self.registry = {}
            self.history = []

        def finalize_outcome(self):
            pass

    original = server_mod.Engine
    original_write = server_mod._write_line
    notifications = []
    server_mod.Engine = FakeEngine
    server_mod._write_line = notifications.append
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = server_mod.SessionManager("fake", Path(tmpdir))
            session = manager._new_session()
            server = await session.engine.artifact_target()
            first_runner = server._runner
            assert await session.engine.artifact_target() is server
            assert server._runner is first_runner, "serve must reuse its artifact listener"
            out = await session.engine.registry["create_artifact"].fn(
                "Serve report", "<p>structured</p>")
            assert "[ok]" in out
            status = manager.artifact_status(session.session_id)
            assert status["server_running"] and len(status["artifacts"]) == 1
            listed = manager.list_sessions()[0]
            assert listed["artifact_count"] == 1
            event = next(n for n in notifications
                         if n.get("method") == "session/artifact_changed")
            assert event["params"]["artifact"]["title"] == "Serve report", event
            assert await manager.stop_artifacts(session.session_id) is True
            assert manager.artifact_status(session.session_id)["server_running"] is False
            await manager.shutdown()
            assert server._runner is None, "serve shutdown must close the artifact listener"
    finally:
        server_mod.Engine = original
        server_mod._write_line = original_write


async def test_no_browser_env(opened: list[str]):
    """ROCKYCODE_ARTIFACT_NO_BROWSER=1 suppresses webbrowser.open on both paths.

    The VS Code extension sets it when spawning serve: it opens the URL from
    the tool output itself, so rocky opening too meant every artifact appeared
    twice. The output lines the extension parses must survive suppression.
    """
    os.environ["ROCKYCODE_ARTIFACT_NO_BROWSER"] = "1"
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)

            # Live path: no open, but the "url:" line the extension parses stays.
            server = ArtifactServer(workdir)
            await server.start()
            try:
                async def target():
                    return server

                tools = build_artifact_tools(workdir=workdir, engine=SimpleNamespace(artifact_target=target))
                out = await tools["create_artifact"].fn(CJK_TITLE, "<p>再见</p>")
                assert "[ok]" in out and "url: " in out, out
            finally:
                await server.stop()

            # Static path: no open, but the "opened in browser: file://" line stays.
            tools = build_artifact_tools(workdir=workdir / "static", engine=None)
            out = await tools["create_artifact"].fn(CJK_TITLE, "<p>再见</p>")
            assert "[ok]" in out and "opened in browser: file://" in out, out

            assert opened == [], f"browser must not open with env set: {opened}"
    finally:
        del os.environ["ROCKYCODE_ARTIFACT_NO_BROWSER"]


async def main() -> None:
    test_safe_filename()
    print("PASS test_safe_filename")

    opened = _capture_opens()
    await test_live_cjk_roundtrip(opened)
    print("PASS test_live_cjk_roundtrip")

    opened.clear()
    await test_static_cjk_file_uri(opened)
    print("PASS test_static_cjk_file_uri")

    opened.clear()
    await test_token_gate(opened)
    print("PASS test_token_gate")

    await test_server_reuse_and_keepalive()
    print("PASS test_server_reuse_and_keepalive")

    await test_session_registry_tracks_browser_clients()
    print("PASS test_session_registry_tracks_browser_clients")

    await test_serve_owns_artifact_lifecycle()
    print("PASS test_serve_owns_artifact_lifecycle")

    opened.clear()
    await test_no_browser_env(opened)
    print("PASS test_no_browser_env")

    print("\nARTIFACT SMOKE OK — CJK links round-trip, endpoints gated")


asyncio.run(main())
