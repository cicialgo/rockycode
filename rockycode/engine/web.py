"""Web tools — chat only (bench stays offline + uncontaminated).

Verified live against the DeepSeek API (2026-06-22):
- OpenAI /v1 endpoint has NO native search (server tools rejected).
- Anthropic /anthropic endpoint runs native server-side search via the
  `web_search_20260209` / `web_search_20250305` server tools. DeepSeek does
  the searching AND page-reading on its servers → works behind restrictive
  networks (client only needs api.deepseek.com). This is the primary backend.
- No server-side fetch tool. `web_fetch(url)` runs client-side and may fail
  on blocked sites — a known limitation; native search covers most needs.

Search backends are tiered and polite:
  native (DeepSeek) → brave (opt-in) → bing (keyless) → duckduckgo (keyless)
Native is the one that matters — and the only one that reliably works behind
restrictive networks: the client only needs api.deepseek.com, and the search
+ page-reading happen on DeepSeek's servers. Among the scrapers, Bing is
ordered before DuckDuckGo because bing.com reaches more restricted regions
(DuckDuckGo is blocked in some). Brave activates ONLY when the user sets
their own BRAVE_API_KEY — we never proxy a shared key (wrong fit for an MIT
project: cost, abuse, liability). Override with
ROCKYCODE_SEARCH_ORDER="native,bing,duckduckgo,brave".

Politeness: an honest identifying User-Agent (no browser impersonation), a
single attempt per backend (no retry storms), and a concurrency cap so
parallel research never floods a service.

`web_research(queries)` fans web_search out across queries in parallel — each
native call is itself a server-side search agent, so this is the "parallel
search" pattern with no local sub-agent loop to run.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from urllib.parse import parse_qs, urlparse

from rockycode.engine.tools import Tool, _fn_schema, _truncate
from rockycode.onboarding import KEY_ENV, current_key, require_base_url

SEARCH_MAX_CHARS = 8_000
ANTHROPIC_VERSION = "2023-06-01"
SEARCH_TOOL_VARIANTS = ("web_search_20260209", "web_search_20250305")
RESEARCH_CONCURRENCY = 4  # polite cap on parallel lookups

# Honest, transparent UA — we say who we are rather than impersonate a browser.
POLITE_UA = "rockycode/0.1 (+https://github.com/cicialgo/rockycode; coding agent web tools)"


# ---- config -----------------------------------------------------------------

def _anthropic_messages_url() -> str:
    explicit = os.getenv("ROCKYCODE_ANTHROPIC_BASE")
    if explicit:
        base = explicit.rstrip("/")
    else:
        v1 = require_base_url().rstrip("/")
        root = v1[:-3].rstrip("/") if v1.endswith("/v1") else v1
        base = root + "/anthropic"
    return base + "/v1/messages"


def _search_model() -> str:
    return os.getenv("ROCKYCODE_SEARCH_MODEL", "deepseek-v4-flash")


def default_search_order() -> tuple[str, ...]:
    env = os.getenv("ROCKYCODE_SEARCH_ORDER")
    if env:
        return tuple(x.strip() for x in env.split(",") if x.strip())
    order = ["native"]
    # Brave only if the user brought their own key — never a proxied/shared one.
    if os.getenv("BRAVE_API_KEY"):
        order.append("brave")
    # Bing before DuckDuckGo: bing.com is reachable from more restricted
    # networks (incl. cn.bing.com); DuckDuckGo is blocked in some regions.
    order += ["bing", "duckduckgo"]
    return tuple(order)


# ---- SSRF guard -------------------------------------------------------------

def _resolve_ips(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


async def _assert_safe_url(url: str) -> list[str]:
    """Block non-http(s) and hosts resolving to private/loopback/link-local/
    reserved addresses (incl. 169.254.169.254 cloud metadata). Returns the
    VALIDATED IPs so the caller can connect to one of them directly — closing
    the DNS-rebind TOCTOU where httpx would otherwise re-resolve the hostname to
    a fresh (internal) address between this check and the socket connect."""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError(f"refusing non-http(s) url: {p.scheme or '?'}")
    if not p.hostname:
        raise ValueError("url has no host")
    try:
        ips = await asyncio.wait_for(asyncio.to_thread(_resolve_ips, p.hostname), timeout=5)
    except asyncio.TimeoutError:
        raise ValueError(f"dns timeout for host: {p.hostname}")
    except socket.gaierror as e:
        raise ValueError(f"cannot resolve host: {e}")
    if not ips:
        raise ValueError(f"no addresses for host: {p.hostname}")
    for raw in ips:
        ip = ipaddress.ip_address(raw)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError(f"refusing to fetch internal address {ip} ({p.hostname})")
    return ips


# ---- formatting -------------------------------------------------------------

def _format_native(text: str, sources: list[dict]) -> str:
    if sources:
        lines = "\n".join(f"- {s.get('title','')} — {s.get('url','')}" for s in sources[:8])
        text = f"{text}\n\nsources:\n{lines}"
    return _truncate(text, SEARCH_MAX_CHARS)


def _format_items(items: list[dict]) -> str:
    if not items:
        return ""
    blocks = [
        f"{i+1}. {it.get('title','')}\n   {it.get('url','')}\n   {it.get('snippet','')}"
        for i, it in enumerate(items)
    ]
    return _truncate("\n".join(blocks), SEARCH_MAX_CHARS)


# ---- backends (each: async query -> formatted str, or raises) ---------------

def _normalize_anthropic_usage(u: dict) -> dict:
    """Anthropic usage → the {prompt_tokens, prompt_cache_hit_tokens,
    completion_tokens} shape the ledger/pricing expects."""
    hit = u.get("cache_read_input_tokens", 0) or 0
    create = u.get("cache_creation_input_tokens", 0) or 0
    inp = u.get("input_tokens", 0) or 0  # anthropic excludes cached from this
    return {
        "prompt_tokens": inp + hit + create,
        "prompt_cache_hit_tokens": hit,
        "completion_tokens": u.get("output_tokens", 0) or 0,
    }


async def _native(query: str, ledger=None) -> str:
    import httpx

    key = current_key()
    if not key:
        raise RuntimeError(f"no API key for native search — set {KEY_ENV}")
    headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    prompt = f"Search the web and answer concisely with key facts: {query}\nCite the sources you used."
    async with httpx.AsyncClient(timeout=120) as client:
        last = None
        for variant in SEARCH_TOOL_VARIANTS:
            r = await client.post(_anthropic_messages_url(), headers=headers, json={
                "model": _search_model(), "max_tokens": 2048,
                "tools": [{"type": variant, "name": "web_search", "max_uses": 5}],
                "messages": [{"role": "user", "content": prompt}],
            })
            if r.status_code == 400 and "unknown variant" in r.text:
                last = r.text
                continue
            r.raise_for_status()
            data = r.json()
            # flash search tokens count toward the session cost too
            if ledger is not None and isinstance(data.get("usage"), dict):
                ledger.add(_search_model(), _normalize_anthropic_usage(data["usage"]))
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            sources = [
                {"title": it.get("title", ""), "url": it["url"]}
                for b in data.get("content", []) if b.get("type") == "web_search_tool_result"
                for it in (b.get("content") or []) if isinstance(it, dict) and it.get("url")
            ]
            return _format_native(text.strip(), sources)
    raise RuntimeError(f"no supported web_search variant: {last}")


async def _brave(query: str) -> str:
    import httpx

    key = os.getenv("BRAVE_API_KEY")
    if not key:
        raise RuntimeError("no BRAVE_API_KEY")
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": 8},
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
        )
    r.raise_for_status()
    results = (r.json().get("web") or {}).get("results", []) or []
    return _format_items([
        {"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("description", "")}
        for x in results
    ])


def _ddg_real_url(href: str) -> str:
    # DDG html wraps links as /l/?uddg=<encoded real url>
    if "duckduckgo.com/l/" in href or href.startswith("//duckduckgo.com/l/"):
        q = parse_qs(urlparse(href).query).get("uddg")
        if q:
            return q[0]
    return href


async def _duckduckgo(query: str) -> str:
    import httpx
    from bs4 import BeautifulSoup

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        r = await client.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query}, headers={"User-Agent": POLITE_UA},
        )
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for res in soup.select(".result")[:8]:
        a = res.select_one("a.result__a")
        if not a or not a.get("href"):
            continue
        sn = res.select_one(".result__snippet")
        items.append({"title": a.get_text(strip=True), "url": _ddg_real_url(a["href"]),
                      "snippet": sn.get_text(strip=True) if sn else ""})
    return _format_items(items)


async def _bing(query: str) -> str:
    import httpx
    from bs4 import BeautifulSoup

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        r = await client.get("https://www.bing.com/search", params={"q": query},
                             headers={"User-Agent": POLITE_UA})
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for li in soup.select("#b_results > li.b_algo")[:8]:
        a = li.select_one("h2 > a") or li.select_one("a.tilk")
        if not a or not a.get("href"):
            continue
        title = a.get("aria-label") or a.get_text(strip=True)
        p = li.select_one('p[class^="b_lineclamp"]') or li.select_one(".b_caption p")
        items.append({"title": title, "url": a["href"], "snippet": p.get_text(strip=True) if p else ""})
    return _format_items(items)


DEFAULT_BACKENDS = {"native": _native, "bing": _bing, "duckduckgo": _duckduckgo, "brave": _brave}


FETCH_MAX_CHARS = 20_000       # cap fetched page text so one page can't flood context
FETCH_MAX_BYTES = 8_000_000    # ~8 MB hard cap on the raw body — OOM guard
MAX_REDIRECTS = 5


def _pinned_request(client, url: str, ip: str):
    """Build a GET that connects to the already-validated *ip* while presenting
    the real hostname (Host header + TLS SNI/cert via the sni_hostname
    extension). This defeats DNS rebinding: httpx never re-resolves the name, so
    it can't be pointed at an internal address after our check."""
    import httpx

    u = httpx.URL(url)
    host_header = u.host if u.port is None else f"{u.host}:{u.port}"
    return client.build_request(
        "GET",
        u.copy_with(host=ip),
        headers={"User-Agent": POLITE_UA, "Host": host_header},
        extensions={"sni_hostname": u.host},
    )


def _too_large(content_length: str) -> bool:
    """True if a declared Content-Length already exceeds the byte cap — lets us
    reject an oversized body up front, before reading a single chunk."""
    return content_length.isdigit() and int(content_length) > FETCH_MAX_BYTES


async def _read_capped(response, cap: int) -> str:
    """Stream the body, stopping once *cap* bytes are read (so a hostile or huge
    response can't exhaust memory), then decode."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        if total + len(chunk) > cap:
            chunks.append(chunk[: cap - total])
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")


async def _web_fetch(url: str, *, _transport=None) -> str:
    import httpx
    from bs4 import BeautifulSoup

    ips = await _assert_safe_url(url)  # resolve+validate once; pin the result
    # follow_redirects=False + a manual loop so EVERY hop is re-validated AND
    # re-pinned: the SSRF guard on the first url is worthless if a 302 can then
    # point at 169.254.169.254 / 127.0.0.1 / a LAN host. httpx would follow blindly.
    client_kwargs = dict(timeout=20, follow_redirects=False)
    if _transport is not None:  # tests inject httpx.MockTransport
        client_kwargs["transport"] = _transport
    async with httpx.AsyncClient(**client_kwargs) as client:
        r = None
        for _ in range(MAX_REDIRECTS):
            r = await client.send(_pinned_request(client, url, ips[0]), stream=True)
            if not r.is_redirect:
                break
            loc = r.headers.get("location")
            if not loc:
                break
            await r.aclose()  # discard redirect body before following
            url = str(httpx.URL(url).join(loc))  # resolve relative Location
            ips = await _assert_safe_url(url)     # re-validate + re-pin BEFORE following
        else:
            if r is not None:
                await r.aclose()
            return "[error] too many redirects"

        try:
            clen = r.headers.get("content-length", "")
            if _too_large(clen):
                return f"[error] response too large ({int(clen):,} bytes; cap {FETCH_MAX_BYTES:,})"
            ctype = r.headers.get("content-type", "").lower()
            body = await _read_capped(r, FETCH_MAX_BYTES)
        finally:
            await r.aclose()

    if "html" not in ctype:
        return _truncate(body, FETCH_MAX_CHARS)
    soup = BeautifulSoup(body, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "form"]):
        tag.decompose()
    return _truncate(soup.get_text("\n", strip=True), FETCH_MAX_CHARS)


# ---- tool factory -----------------------------------------------------------

def build_web_tools(
    *,
    ledger=None,
    search_order: tuple[str, ...] | None = None,
    backends: dict | None = None,
    fetch_fn=_web_fetch,
) -> dict[str, Tool]:
    """The three web tools. backends/fetch are injectable so tests run offline.
    `ledger` (a pricing.UsageLedger) captures flash search-token usage so it
    counts toward the session cost."""
    order = search_order or default_search_order()
    # native search reports its flash usage into the ledger
    bk = backends or {
        "native": lambda q: _native(q, ledger=ledger),
        "bing": _bing, "duckduckgo": _duckduckgo, "brave": _brave,
    }
    sem = asyncio.Semaphore(RESEARCH_CONCURRENCY)

    def _today() -> str:
        from datetime import date
        return date.today().isoformat()

    async def web_search(query: str) -> str:
        errors = []
        for name in order:
            fn = bk.get(name)
            if fn is None:
                continue
            try:
                result = await fn(query)
                if result and result.strip():
                    # the date belongs at the point of recency judgment —
                    # and it stays true even when a session crosses midnight
                    return f"[searched {_today()}] {result}"
            except Exception as e:  # noqa: BLE001 — try next backend
                errors.append(f"{name}: {type(e).__name__}: {e}")
        return f"[error] all search backends failed — {' | '.join(errors) or 'no results'}"

    async def web_research(queries: list[str]) -> str:
        if not isinstance(queries, list) or not queries:
            return "[error] web_research needs a non-empty list of queries"

        async def one(q):
            async with sem:  # polite: cap concurrent lookups
                return await web_search(q)

        results = await asyncio.gather(*(one(q) for q in queries[:8]), return_exceptions=True)
        out = [f"### {q}\n{('[error] ' + str(r)) if isinstance(r, Exception) else r}"
               for q, r in zip(queries, results)]
        return _truncate("\n\n".join(out), SEARCH_MAX_CHARS * 2)

    async def web_fetch(url: str) -> str:
        try:
            return f"[fetched {_today()}] {await fetch_fn(url)}"
        except Exception as e:  # noqa: BLE001 — model-readable
            return f"[error] fetch failed: {type(e).__name__}: {e}"

    schemas = {
        "web_search": _fn_schema(
            "web_search",
            "Search the web for current information; returns a concise answer with sources. "
            "Runs server-side on DeepSeek (works behind restrictive networks).",
            {"query": {"type": "string", "description": "What to search for."}},
            ["query"],
        ),
        "web_research": _fn_schema(
            "web_research",
            "Search several independent questions at once, in parallel. Faster than "
            "sequential web_search calls when a task needs multiple lookups.",
            {"queries": {"type": "array", "items": {"type": "string"},
                         "description": "Independent search queries (max 8)."}},
            ["queries"],
        ),
        "web_fetch": _fn_schema(
            "web_fetch",
            "Fetch one URL and return its readable text. Runs on this machine, so it may "
            "fail on sites your network blocks; prefer web_search for general info.",
            {"url": {"type": "string", "description": "The http(s) URL to fetch."}},
            ["url"],
        ),
    }
    fns = {"web_search": web_search, "web_research": web_research, "web_fetch": web_fetch}
    # search/web_research run server-side on DeepSeek (read-only); web_fetch hits the
    # local network from this machine (SSRF-guarded) — gate it like other risky I/O.
    risk = {"web_search": "safe", "web_research": "safe", "web_fetch": "risky"}
    return {
        name: Tool(name=name, schema=schemas[name], fn=fns[name], risk=risk[name])
        for name in fns
    }
