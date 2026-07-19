"""Artifact tool — generate a self-contained HTML page and open it.

One tool: ``create_artifact``.  The model supplies body content (or a full
document); rockycode wraps a fragment in its themed page and opens it.

Static (default):  write the file and open it via ``file://`` — no server.
Live (opt-in):     ``ArtifactServer`` serves it over ``http://localhost`` and
                   the open tab auto-reloads whenever rocky rebuilds the same
                   artifact.  Lazy-started on the first artifact (asked once),
                   or from the start with ``--live``.  Local only.

The TUI sets ``engine.artifact_target`` — an async callable that decides live
vs static (asking once), lazy-starts the server, and returns the running
``ArtifactServer`` (live) or ``None`` (static).
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import webbrowser
from pathlib import Path
from urllib.parse import quote, unquote

from rockycode.engine.tools import Tool, _fn_schema

ARTIFACT_DIR = ".rockycode/artifacts"
MAX_ARTIFACT_CHARS = 500_000  # 500 KB — generous for inline-SVG/data-URI HTML


def _open_browser(url: str) -> None:
    """Best-effort browser open.

    Suppressed when ROCKYCODE_ARTIFACT_NO_BROWSER=1: an embedding editor (the
    VS Code extension) sets it because it opens the URL from the tool output
    itself — without this, every artifact opened twice. The output lines
    ("url: ...", "opened in browser: file://...") are the extension's parse
    surface and must keep their format either way.
    """
    if os.getenv("ROCKYCODE_ARTIFACT_NO_BROWSER") == "1":
        return
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 — best-effort
        pass


def _safe_filename(name: str) -> str:
    """Sanitise *name* into a safe file stem (<=80 chars)."""
    safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in name)
    return safe.strip().replace(" ", "_")[:80] or "artifact"


def _extract_body(html: str) -> str:
    """Reduce model HTML to themable body content.

    The model often emits a whole <html> document with its own (usually dark)
    theme — DeepSeek loves to. We keep only the inner <body> and strip every
    <style> block, so rocky's light-purple theme always wins. A plain fragment
    is returned with just its <style> blocks removed.
    """
    low = html.lower()
    i = low.find("<body")
    if i != -1:
        start = low.find(">", i)
        end = low.rfind("</body>")
        if start != -1 and end != -1:
            html = html[start + 1:end]
    return re.sub(r"(?is)<style\b.*?</style>", "", html)


def _artifact_html(title: str, body: str) -> str:
    """Wrap *body* in rockycode's light, soft-purple themed page.

    Palette tracks rockycode/palette.py + ROCKY_THEME_LIGHT.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
:root {{ --bg:#f6f4fb; --card:#ffffff; --panel:#ece8f7; --border:#ddd4ef;
       --text:#2a2a38; --dim:#5a5e7a; --heading:#6a4ca3; --accent:#7c5cba; --brand:#bb9af7; }}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
       background:var(--bg); color:var(--text); line-height:1.6; max-width:900px; margin:0 auto; padding:2rem; }}
h1 {{ color:var(--heading); border-bottom:2px solid var(--border); padding-bottom:.5rem; margin-bottom:.75rem; font-weight:650; }}
h2 {{ color:var(--accent); margin:1.5rem 0 .5rem; font-size:1.15rem; }}
.card {{ background:var(--card); border:1px solid var(--border); border-radius:10px;
        padding:1rem 1.1rem; margin:1rem 0; box-shadow:0 1px 3px rgba(106,76,163,0.06); }}
.tag {{ display:inline-block; padding:.1rem .55rem; border-radius:10px; font-size:.72rem; font-weight:600; }}
.tag-purple {{ background:#efe9fb; color:#6a4ca3; }}
.tag-amber {{ background:#f7efe0; color:#8a6420; }}
.tag-red {{ background:#fbe9eb; color:#b23a44; }}
table {{ width:100%; border-collapse:collapse; margin:.5rem 0; }}
th,td {{ border:1px solid var(--border); padding:.45rem .65rem; text-align:left; font-size:.85rem; }}
th {{ background:var(--panel); color:var(--heading); }}
pre {{ background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:.85rem; overflow-x:auto; font-size:.82rem; }}
code {{ font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace; color:var(--heading); }}
a {{ color:var(--accent); }}
</style>
</head>
<body>
<h1>{title}</h1>
{body}
</body>
</html>"""


def _reload_script(sse_url: str, name: str) -> str:
    """A tiny SSE client: reload this page when its artifact is rebuilt."""
    return f"""
<script>
(function() {{
  try {{
    const es = new EventSource("{sse_url}");
    es.addEventListener("reload", function(e) {{
      let d = {{}}; try {{ d = JSON.parse(e.data); }} catch(_) {{}}
      if (!d.name || d.name === "{name}") location.reload();
    }});
  }} catch(_) {{}}
}})();
</script>"""


def _inject_reload(doc: str, sse_url: str, name: str) -> str:
    """Insert the live-reload script before </body> (or append)."""
    script = _reload_script(sse_url, name)
    idx = doc.lower().rfind("</body>")
    return doc[:idx] + script + doc[idx:] if idx != -1 else doc + script


def build_artifact_tools(*, workdir: Path, engine=None) -> dict[str, Tool]:
    """Return {create_artifact} bound to *workdir*.

    At call time the tool consults ``engine.artifact_target`` (set by the TUI)
    to decide live vs static and obtain the running server.
    """
    artifact_dir = workdir / ARTIFACT_DIR

    async def create_artifact(title: str, html: str) -> str:
        """Save a self-contained HTML artifact and open it in the browser."""
        if len(html) > MAX_ARTIFACT_CHARS:
            return (
                f"[error] artifact too large: {len(html):,} chars "
                f"(max {MAX_ARTIFACT_CHARS:,}).  simplify the content."
            )
        artifact_dir.mkdir(parents=True, exist_ok=True)

        # The TUI decides live vs static (asking once on first use, lazy-starting
        # the server). Headless/bench have no target → static file://.
        target = getattr(engine, "artifact_target", None)
        server = await target() if target is not None else None

        stem = _safe_filename(title)
        doc = _artifact_html(title, _extract_body(html))

        if server is not None:
            # Live: stable name, overwrite in place, auto-reload the open tab.
            path = artifact_dir / f"{stem}.html"
            existed = path.exists()
            doc = _inject_reload(doc, f"{server.base_url}/api/events?t={server.token}", stem)
            path.write_text(doc, encoding="utf-8")
            # Percent-encode: CJK stems survive _safe_filename (isalnum), and a
            # raw non-ASCII URL gets mangled by webbrowser/terminal handlers.
            # ?t= is the per-session nonce every server endpoint requires.
            url = f"{server.base_url}/artifacts/{quote(stem)}?t={server.token}"
            if existed:
                await server.broadcast("reload", {"name": stem})  # open tab refreshes
                return f"[ok] artifact '{title}' updated live — {len(doc):,} chars\n  url: {url}"
            _open_browser(url)
            return f"[ok] artifact '{title}' opened live — {len(doc):,} chars\n  url: {url}"

        # Static: unique filename, file://.
        path = artifact_dir / f"{stem}.html"
        n = 1
        while path.exists():
            path = artifact_dir / f"{stem}_{n}.html"
            n += 1
        path.write_text(doc, encoding="utf-8")
        file_uri = path.resolve().as_uri()
        _open_browser(file_uri)
        return (
            f"[ok] artifact saved — {len(doc):,} chars\n"
            f"  file: {path}\n"
            f"  opened in browser: {file_uri}"
        )

    schema = _fn_schema(
        "create_artifact",
        "Create a visual artifact (report, diagram, dashboard, summary) and open "
        "it in the browser. Use for output better seen outside the terminal. "
        "Provide the BODY content ONLY — headings (<h1>/<h2>), paragraphs, cards "
        "(<div class='card'>), tables, <pre>/<code>, inline SVG/Mermaid. Do NOT "
        "write <html>/<head>/<style>, and do NOT set your own colors or "
        "background — rockycode applies its own LIGHT theme (soft purple on "
        "near-white). Use the provided classes: card, tag/tag-purple/tag-amber/"
        "tag-red. No external CDN/network references. Reuse the SAME title to "
        "update in place (in live mode the open tab auto-refreshes).",
        {
            "title": {
                "type": "string",
                "description": "Short descriptive title (also the filename; reuse to update).",
            },
            "html": {
                "type": "string",
                "description": (
                    "Body content only — no <html>/<head>/<style> and no color/background "
                    "styles (rocky themes it). Use rocky's classes; embed SVG/Mermaid inline."
                ),
            },
        },
        ["title", "html"],
    )
    return {"create_artifact": Tool(name="create_artifact", schema=schema, fn=create_artifact)}


# ------------------------------------------------------------------
# ArtifactServer — live mode (host browser reaches localhost)
# ------------------------------------------------------------------

class ArtifactServer:
    """Serve artifact files over localhost + an SSE stream for live reload.

    Every endpoint requires the per-session nonce (``?t=<token>``, audit #7):
    localhost services are reachable by any local process and — via the
    browser — probeable by any web page the user has open, so unauthenticated
    ``/list``/``/api/events`` would leak artifact names and reload activity.
    The token is baked into every URL rocky emits (open link, /list entries,
    the injected reload script); it never needs to be typed.

    Binding: 127.0.0.1 by default. Inside Docker (ROCKYCODE_IN_DOCKER) it must
    bind 0.0.0.0 — Docker can't forward a published port to the container's
    127.0.0.1 — so the compose/run mapping MUST publish the port on host
    loopback only (``127.0.0.1:PORT:PORT``, as docker-compose.yml does);
    publishing bare ``PORT:PORT`` would expose the server to the LAN.
    Lifecycle: ``await start()`` when live mode turns on, ``await stop()`` on
    exit.
    """

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.artifact_dir = workdir / ARTIFACT_DIR
        self.port: int = 0
        # Unguessable per-session nonce gating every endpoint (see class doc).
        self.token: str = secrets.token_urlsafe(16)
        self._app: "web.Application | None" = None
        self._runner: "web.AppRunner | None" = None
        self._sse_queues: set["asyncio.Queue"] = set()

    def _require_token(self, request: "web.Request") -> None:
        """401 unless the request carries the session token (?t=...)."""
        from aiohttp import web

        if not secrets.compare_digest(request.query.get("t", ""), self.token):
            raise web.HTTPUnauthorized(text="missing or invalid artifact token")

    async def broadcast(self, event_type: str, data: dict | None = None) -> None:
        """Push an SSE event to all connected clients."""
        import json as _json
        chunk = f"event: {event_type}\ndata: {_json.dumps(data or {})}\n\n"
        for q in list(self._sse_queues):
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                self._sse_queues.discard(q)

    async def start(self) -> None:
        from aiohttp import web

        self._app = web.Application()
        self._app.router.add_get("/artifacts/{name}", self._serve_artifact)
        self._app.router.add_get("/list", self._list_artifacts)
        self._app.router.add_get("/api/events", self._handle_events)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        # Host: loopback. Docker: 0.0.0.0 is required (the published port forwards
        # to the container bridge, not its 127.0.0.1) — but compose binds the host
        # side to 127.0.0.1, so the server is never LAN-exposed.
        bind_host = "0.0.0.0" if os.environ.get("ROCKYCODE_IN_DOCKER") else "127.0.0.1"
        fixed_port = int(os.environ["ROCKYCODE_ARTIFACT_PORT"]) if "ROCKYCODE_ARTIFACT_PORT" in os.environ else 0
        site = web.TCPSite(self._runner, bind_host, fixed_port)
        await site.start()
        for sock in site._server.sockets:
            self.port = sock.getsockname()[1]
            break

    @property
    def base_url(self) -> str:
        return f"http://localhost:{self.port}"

    async def _serve_artifact(self, request: "web.Request") -> "web.Response":
        from aiohttp import web

        self._require_token(request)
        # unquote: percent-encoded requests must match on-disk stems (which are
        # stored decoded, CJK and all). Stems never contain '%' (_safe_filename
        # maps it to '_'), so decoding an already-decoded name is a no-op.
        name = unquote(request.match_info.get("name", "index"))
        safe = _safe_filename(name)
        if safe != name:
            raise web.HTTPBadRequest(text="invalid artifact name")
        fpath = self.artifact_dir / f"{safe}.html"
        if not fpath.exists():
            raise web.HTTPNotFound(text="artifact not found")
        return web.FileResponse(fpath, headers={"Content-Type": "text/html; charset=utf-8"})

    async def _list_artifacts(self, request: "web.Request") -> "web.Response":
        from aiohttp import web

        self._require_token(request)
        files = sorted(self.artifact_dir.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
        items = [
            {"name": f.stem, "size": f.stat().st_size,
             "url": f"{self.base_url}/artifacts/{quote(f.stem)}?t={self.token}"}
            for f in files[:50]
        ]
        return web.json_response(items)

    async def _handle_events(self, request: "web.Request") -> "web.StreamResponse":
        """SSE endpoint — streams reload events to open artifact tabs.

        No CORS header: the only consumer is the reload script injected into
        artifact pages served from this same origin. A wildcard here let any
        web page EventSource the stream cross-origin (audit #7).
        """
        from aiohttp import web

        self._require_token(request)
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)
        queue: "asyncio.Queue" = asyncio.Queue(maxsize=64)
        self._sse_queues.add(queue)
        try:
            await resp.write(b"event: connected\ndata: {}\n\n")
            while True:
                chunk = await asyncio.wait_for(queue.get(), timeout=30)
                await resp.write(chunk.encode())
        except asyncio.TimeoutError:
            try:
                await resp.write(b": keepalive\n\n")
            except (ConnectionResetError, ConnectionError):
                pass
        except (ConnectionResetError, ConnectionError):
            pass
        finally:
            self._sse_queues.discard(queue)
        return resp

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._app = None
