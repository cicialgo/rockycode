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
from rockycode.engine.artifact import ARTIFACT_DIR, ArtifactServer, _safe_filename, build_artifact_tools

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

    opened.clear()
    await test_no_browser_env(opened)
    print("PASS test_no_browser_env")

    print("\nARTIFACT SMOKE OK — CJK links round-trip, endpoints gated")


asyncio.run(main())
