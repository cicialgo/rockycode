"""Web tools smoke test — offline (injected fake backends, no network/tokens).

Covers: native search path, bing fallback when native is empty/fails, parallel
web_research fan-out, web_fetch, and the SSRF guard rejecting private addresses.
"""
import asyncio

from rockycode.engine import tools as tools_mod
from rockycode.engine import web


async def main():
    calls = {"native": 0, "bing": 0, "fetch": 0}

    async def fake_native(query):
        calls["native"] += 1
        if "fallback" in query:
            raise RuntimeError("native down")
        return {"text": f"answer about {query}", "sources": [{"title": "T", "url": "https://ex.com/a"}]}

    async def fake_bing(query):
        calls["bing"] += 1
        return [{"title": "B", "url": "https://b.com", "snippet": "snip"}]

    async def fake_fetch(url):
        calls["fetch"] += 1
        return f"text of {url}"

    async def fake_native_str(query):
        d = await fake_native(query)
        return web._format_native(d["text"], d["sources"])

    async def fake_bing_str(query):
        items = await fake_bing(query)
        return web._format_items(items)

    backends = {"native": fake_native_str, "bing": fake_bing_str}
    reg = web.build_web_tools(search_order=("native", "bing"), backends=backends, fetch_fn=fake_fetch)
    assert set(reg) == {"web_search", "web_research", "web_fetch"}, set(reg)

    # native path
    out, ok = await tools_mod.execute(reg, "web_search", '{"query": "python version"}')
    assert ok and "answer about python version" in out and "ex.com/a" in out, out
    assert calls["native"] == 1 and calls["bing"] == 0

    # fallback to bing when native raises
    out, ok = await tools_mod.execute(reg, "web_search", '{"query": "fallback please"}')
    assert ok and "b.com" in out, out
    assert calls["bing"] == 1, calls

    # web_research fans out in parallel (3 queries -> 3 native calls)
    before = calls["native"]
    out, ok = await tools_mod.execute(reg, "web_research", '{"queries": ["a", "b", "c"]}')
    assert ok and "### a" in out and "### c" in out, out
    assert calls["native"] - before == 3, calls

    # web_research arg validation
    out, ok = await tools_mod.execute(reg, "web_research", '{"queries": []}')
    assert "[error]" in out, out

    # web_fetch
    out, ok = await tools_mod.execute(reg, "web_fetch", '{"url": "https://example.com"}')
    assert ok and "text of https://example.com" in out, out

    # SSRF guard: real _web_fetch must reject private/loopback hosts
    real = web.build_web_tools()
    for bad in ["http://127.0.0.1/x", "http://localhost/x", "http://169.254.169.254/latest/meta-data",
                "http://10.0.0.5/x", "file:///etc/passwd"]:
        out, ok = await tools_mod.execute(real, "web_fetch", '{"url": "%s"}' % bad)
        assert "[error]" in out, (bad, out)
    print("SSRF guard rejected all private/loopback/metadata/file URLs")

    await _test_pinning()
    await _test_redirect_repin()
    await _test_body_cap()
    print("WEB SMOKE OK — amaze amaze amaze!")


# ── #2/#4: DNS-rebind pinning + OOM cap (offline via httpx.MockTransport) ──

import httpx  # noqa: E402

_PUBLIC = "93.184.216.34"   # example.com — passes the SSRF allowlist


def _pin(mapping):
    """Patch resolution so a hostname maps to a chosen (public, allowlisted) IP,
    and _assert_safe_url returns it for pinning."""
    web._resolve_ips = lambda host: mapping.get(host, [_PUBLIC])


async def _test_pinning():
    _pin({"example.com": [_PUBLIC]})
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["conn_host"] = request.url.host                    # who we actually connect to
        seen["host_hdr"] = request.headers.get("host")           # what we present
        seen["sni"] = request.extensions.get("sni_hostname")     # TLS cert/SNI hostname
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="pinned body")

    out = await web._web_fetch("https://example.com/p", _transport=httpx.MockTransport(handler))
    assert "pinned body" in out, out
    # Connected to the VALIDATED IP, never re-resolving the name (rebind defeated);
    # Host + SNI still carry the real hostname so vhost routing + TLS work.
    assert seen["conn_host"] == _PUBLIC, seen
    assert seen["host_hdr"] == "example.com", seen
    assert seen["sni"] == "example.com", seen
    print("web_fetch: connects to the validated IP; Host+SNI keep the hostname  ✓")


async def _test_redirect_repin():
    _pin({"start.test": ["93.184.216.34"], "end.test": ["93.184.216.35"]})
    hops = []

    def handler(request: httpx.Request) -> httpx.Response:
        hh = request.headers.get("host")
        hops.append((hh, request.url.host))
        if hh == "start.test":
            return httpx.Response(302, headers={"location": "http://end.test/final"})
        if hh == "end.test":
            return httpx.Response(200, headers={"content-type": "text/plain"}, text="arrived")
        return httpx.Response(500)

    out = await web._web_fetch("http://start.test/", _transport=httpx.MockTransport(handler))
    assert "arrived" in out, out
    # Each hop was re-validated AND re-pinned to its own resolved IP.
    assert ("start.test", "93.184.216.34") in hops and ("end.test", "93.184.216.35") in hops, hops
    print("web_fetch: redirect hop is re-validated and re-pinned  ✓")


async def _test_body_cap():
    # oversized Content-Length rejected up front
    assert web._too_large(str(web.FETCH_MAX_BYTES + 1)) and not web._too_large("100") and not web._too_large("")

    _pin({"big.test": [_PUBLIC]})
    huge = "A" * (web.FETCH_MAX_BYTES + 5_000_000)  # ~13 MB > 8 MB cap

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, text=huge)

    out = await web._web_fetch("http://big.test/", _transport=httpx.MockTransport(handler))
    # streamed read stops at the byte cap, then text is truncated to the char cap
    assert len(out) <= web.FETCH_MAX_CHARS + 200, len(out)
    assert out != huge, "body was not capped"
    print("web_fetch: oversized body capped (stream cap + Content-Length guard)  ✓")


asyncio.run(main())
