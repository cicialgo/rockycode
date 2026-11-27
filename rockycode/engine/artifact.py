"""Versioned HTML artifacts on rocky's own page, with host-owned preview.

A fragment gets rocky's page (engine/artifact_page.py: one stylesheet, the
user's look, light and dark); a full HTML document is kept as written. Saved
revisions contain no preview credentials. Live HTTP responses inject SSE
reload support, while static files and exported originals stay self-contained.
"""
from __future__ import annotations

import asyncio
import os
import re
import json
import sqlite3
from html import escape as html_escape
import secrets
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from urllib.parse import quote, unquote

if TYPE_CHECKING:
    from aiohttp import web

from rockycode.engine.artifact_page import DEFAULT_LOOK, LAYOUTS, NO_LOOK, THEMES, page_css, resolve_theme
from rockycode.engine.tools import Tool, _fn_schema

ARTIFACT_DIR = ".rockycode/artifacts"
MAX_ARTIFACT_CHARS = 500_000  # 500 KB — generous for inline-SVG/data-URI HTML
SSE_KEEPALIVE_S = 30.0


@dataclass
class ArtifactRecord:
    """One artifact created by the current engine session."""

    name: str
    title: str
    path: Path
    url: str
    live: bool
    created_at: float
    updated_at: float
    open_clients: int = 0
    artifact_id: str = ""
    revision: int = 1
    theme: str = "none"
    layout: str = "document"
    sessions: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "artifact_id": self.artifact_id,
            "revision": self.revision,
            "theme": self.theme,
            "layout": self.layout,
            "sessions": list(self.sessions),
            "name": self.name,
            "title": self.title,
            "path": str(self.path),
            "url": self.url,
            "live": self.live,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "open_clients": self.open_clients,
        }


class ArtifactRegistry:
    """Project inventory with persistent versions and session-filtered views.

    Browser state stays in memory; saved revisions and their provenance survive
    restarts. A no-workdir registry remains available to lightweight hosts/tests.
    """

    def __init__(self, workdir: Path | None = None, session_id: str = "") -> None:
        from rockycode.engine.artifact_store import ArtifactStore
        self.store = ArtifactStore(workdir) if workdir is not None else None
        self.session_id = session_id
        self._records: dict[str, ArtifactRecord] = {}
        self._listeners: list[Callable[[str, ArtifactRecord], None]] = []
        self.refresh()

    def subscribe(self, listener: Callable[[str, ArtifactRecord], None]) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def refresh(self) -> None:
        if self.store is None:
            return
        for row in self.store.records():
            record = self._records.get(row['name'])
            if record is None:
                record = ArtifactRecord(row['name'], row['title'], row['path'],
                                        row['path'].as_uri(), False, row['created'], row['updated'])
                self._records[record.name] = record
            record.title, record.path = row['title'], row['path']
            record.artifact_id, record.revision = row['id'], row['revision']
            record.created_at, record.updated_at = row['created'], row['updated']
            record.theme, record.layout, record.sessions = row['theme'], row['layout'], row['sessions']
            if not record.live:
                record.url = record.path.as_uri()

    def resume_from(self, session_id: str) -> None:
        if self.store is not None:
            self.store.resume_from(self.session_id, session_id)

    def _emit(self, action: str, record: ArtifactRecord) -> None:
        for listener in list(self._listeners):
            try:
                listener(action, record)
            except Exception:  # noqa: BLE001 — UI observers must not break the tool
                pass

    def upsert(self, *, name: str, title: str, path: Path,
               url: str, live: bool) -> ArtifactRecord:
        now = time.time()
        path = path.resolve()
        record = self._records.get(name)
        action = "updated" if record is not None else "created"
        if record is None:
            record = ArtifactRecord(
                name=name, title=title, path=path, url=url, live=live,
                created_at=now, updated_at=now,
            )
            self._records[name] = record
        else:
            record.title = title
            record.path = path
            record.url = url
            record.live = live
            record.updated_at = now
        self._emit(action, record)
        return record

    def set_client_connected(self, name: str, connected: bool) -> None:
        record = self._records.get(name)
        if record is None:
            return
        delta = 1 if connected else -1
        record.open_clients = max(0, record.open_clients + delta)
        self._emit("clients", record)

    def get(self, name: str) -> ArtifactRecord | None:
        self.refresh()
        return self._records.get(name) or next(
            (r for r in self._records.values() if r.artifact_id == name), None)

    def records(self, scope: str = "project") -> list[ArtifactRecord]:
        self.refresh()
        rows = list(self._records.values())
        if scope == "session" and self.store is not None:
            sessions = self.store.session_scope(self.session_id)
            rows = [r for r in rows if sessions.intersection(r.sessions)]
        return sorted(rows, key=lambda r: (-r.updated_at, r.name))

    def snapshot(self, scope: str = "project") -> list[dict]:
        return [record.as_dict() for record in self.records(scope)]

    def history(self, artifact_id: str) -> list[dict]:
        return self.store.history(artifact_id) if self.store else []

    def rebind_live(self, base_url: str, token: str) -> None:
        # Tokens only enter the served response, never the saved/exported HTML.
        for record in self.records():
            if record.live:
                record.url = f"{base_url}/artifacts/{quote(record.name)}?t={token}"
                self._emit("server", record)

    @property
    def count(self) -> int:
        """Artifacts this session made or changed: what a status line counts.
        The whole project's inventory is records() and the picker."""
        return len(self.records("session"))

    @property
    def open_clients(self) -> int:
        return sum(record.open_clients for record in self._records.values())


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
    """Compatibility helper: preserve authored styles and complete documents."""
    return html


def _with_charset(doc: str) -> str:
    """A full document that declares no charset gets one, and nothing else changes.

    The file is written as UTF-8. Opened from disk there is no HTTP header to say
    so, and a browser left to guess can show Chinese text as garbage.
    """
    if re.search(r"(?is)<meta\b[^>]*\bcharset\b", doc):
        return doc
    meta = '<meta charset="utf-8">'
    for tag in (r"<head\b[^>]*>", r"<html\b[^>]*>", r"<!doctype\b[^>]*>"):
        found = re.search("(?is)" + tag, doc)
        if found:
            return doc[:found.end()] + meta + doc[found.end():]
    return meta + doc


def default_theme(workdir: Path | None = None) -> str:
    """The look a new fragment gets when the model names none: the user's
    `artifact_theme` setting (their own file, or this project's)."""
    from rockycode.config import load
    try:
        return resolve_theme(load(workdir)["artifact_theme"])
    except (KeyError, ValueError):
        return DEFAULT_LOOK


def _artifact_html(title: str, body: str, theme: str = DEFAULT_LOOK, layout: str = "document") -> str:
    """A full HTML document is kept as written, apart from a charset declaration
    when it has none. A fragment is put on rocky's page: the look's colours and
    type in light and dark, at zero specificity, so CSS the fragment brings
    wins. theme `none` is the page without colours, for a fragment that brings
    its own palette.
    """
    theme = resolve_theme(theme)
    if layout not in LAYOUTS:
        raise ValueError(f"layout must be one of {', '.join(LAYOUTS)}")
    if re.search(r"(?is)<(?:!doctype\s+html|html\b|head\b|body\b)", body):
        return _with_charset(body)
    if theme == NO_LOOK:
        width = "1440px" if layout == "wide" else "900px"
        css = f""":where(body) {{ font-family:system-ui,sans-serif; line-height:1.65; max-width:{width};
  margin:auto; padding:clamp(1rem,3vw,2.5rem); }}
:where(table) {{ border-collapse:collapse; width:100%; }}
:where(th,td) {{ border-bottom:1px solid; padding:.55rem .75rem; text-align:left; }}
:where(pre) {{ overflow:auto; }}
:where(img,svg) {{ max-width:100%; }}
"""
    else:
        css = page_css(theme, layout)
    heading = "" if re.search(r"(?is)<h1\b", body) else f"<h1>{html_escape(title)}</h1>"
    return (f'<!DOCTYPE html>\n<html data-layout="{layout}" data-rocky-page="{theme}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html_escape(title)}</title><style>{css}</style></head><body>{heading}{body}</body></html>')


# What _artifact_html writes around a fragment. The title is escaped and the page's
# stylesheet is the head's only <style>, so the first `</style></head><body>` is ours.
_ON_PAGE = re.compile(r'(?s)\A<!DOCTYPE html>\n<html data-layout="[\w-]+" data-rocky-page="[\w-]+"><head>'
                      r'.*?</style></head><body>(.*)</body></html>\Z')


def page_fragment(doc: str) -> str | None:
    """The fragment inside a document rocky's page was put around, or None for
    any other document. A model that edits an artifact reads the fragment and
    sends a fragment back: the stylesheet is not re-sent with every revision,
    and the artifact stays on the page instead of freezing into a full document."""
    found = _ON_PAGE.match(doc)
    return found.group(1) if found else None


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
    """Versioned project artifacts; the host chooses whether/how to preview."""
    from rockycode.engine.artifact_store import ArtifactStore

    session_id = getattr(getattr(engine, "trajectory", None), "session_id", "") or secrets.token_hex(8)
    registry = getattr(engine, "artifact_registry", None)
    if registry is None:
        registry = ArtifactRegistry(workdir, session_id)
        if engine is not None:
            engine.artifact_registry = registry
    elif registry.store is None:
        registry.store, registry.session_id = ArtifactStore(workdir), session_id
        registry.refresh()
    resumed = getattr(engine, "artifact_resumed_from", None)
    if resumed:
        registry.resume_from(resumed)

    async def create_artifact(title: str, html: str, artifact_id: str | None = None,
                              base_revision: int | None = None, theme: str | None = None,
                              layout: str | None = None) -> str:
        """Create a distinct work, or publish a new revision by ID + base revision."""
        if len(html) > MAX_ARTIFACT_CHARS:
            return f"[error] artifact too large: {len(html):,} chars (max {MAX_ARTIFACT_CHARS:,})"
        try:
            previous = registry.get(artifact_id) if artifact_id else None
            # A revision keeps the look its artifact has; a new one gets the user's default.
            chosen_theme = resolve_theme(theme if theme is not None
                                         else (previous.theme if previous else default_theme(workdir)))
            chosen_layout = layout if layout is not None else (previous.layout if previous else "document")
            doc = _artifact_html(title, html, chosen_theme, chosen_layout)
            aid = registry.store.save(title=title, document=doc, session_id=registry.session_id,
                                      theme=chosen_theme, layout=chosen_layout,
                                      artifact_id=artifact_id, base_revision=base_revision)
            record = registry.get(aid)
        except (OSError, ValueError, sqlite3.Error) as exc:
            return f"[error] artifact not saved: {exc}"
        # A preview startup failure must never discard a successful save.
        server, preview_warning = None, ""
        target = getattr(engine, "artifact_target", None)
        if target is not None:
            try:
                server = await target()
            except (OSError, ValueError, RuntimeError) as exc:
                preview_warning = f"\n  preview unavailable (saved file is intact): {exc}"
        record.live = server is not None
        record.url = (f"{server.base_url}/artifacts/{quote(record.name)}?t={server.token}"
                      if server else record.path.as_uri())
        if server is not None:
            server.registry = registry
        registry._emit("updated" if artifact_id else "created", record)
        # A live page that is already open reloads itself. Anything else is a new
        # URL (each revision is its own file), so a host that presents artifacts
        # itself gets it handed over. The TUI shows a card and opens nothing.
        reloaded = server is not None and bool(artifact_id)
        if reloaded:
            await server.broadcast("reload", {"name": record.name})
        presented = not reloaded and getattr(engine, "artifact_auto_open", True)
        if presented:
            opener = getattr(engine, "artifact_opener", None) or _open_browser
            opener(record.url)
        result = (f"[ok] artifact '{title}' saved · v{record.revision}\n"
                  f"  artifact_id: {aid}\n  revision: {record.revision}\n  file: {record.path}\n")
        # "url:" and "opened in browser:" are the VS Code extension's parse surface.
        # The third line is for the model: nothing was opened, so it must not say so.
        if server is not None:
            result += f"  url: {record.url}"
        elif presented:
            result += f"  opened in browser: {record.url}"
        else:
            result += f"  not opened — the user opens it when they choose: {record.url}"
        return result + preview_warning

    async def inspect_artifact(artifact_id: str = "", revision: int | None = None) -> str:
        """Discover IDs after restart and read a particular revision for editing."""
        if revision is not None and (not isinstance(revision, int) or revision < 1):
            return "[error] revision must be a positive integer"
        if not artifact_id:
            return json.dumps([r.as_dict() for r in registry.records()], ensure_ascii=False)
        record = registry.get(artifact_id)
        if record is None:
            return "[error] unknown artifact_id"
        history = registry.history(record.artifact_id)
        selected = next((r for r in history if r['revision'] == (revision or record.revision)), None)
        if selected is None:
            return "[error] unknown revision"
        try:
            content = selected['path'].read_text(encoding='utf-8')
        except OSError as exc:
            return f"[error] artifact file unavailable: {exc}"
        fragment = page_fragment(content)
        hint = "If truncated, read the saved path in chunks before editing."
        if fragment is not None:
            # On rocky's page: the model gets what it wrote, and sends a fragment back.
            content = fragment
            hint = ("This is a fragment on rocky's page: send a fragment back, not a full document, and it keeps "
                    "its look. If truncated, read the saved path in chunks; the fragment is what is inside <body>.")
        return json.dumps({**record.as_dict(), "revision": selected['revision'],
                           "title": selected['title'], "path": str(selected['path']),
                           "theme": selected['theme'], "layout": selected['layout'],
                           "history": [{k: v for k, v in r.items() if k != 'path'} for r in history],
                           "html": content[:20000], "truncated": len(content) > 20000,
                           "fragment": fragment is not None,
                           "current_revision": record.revision,
                           "read_hint": hint}, ensure_ascii=False)

    schema = _fn_schema("create_artifact",
        "Create or revise a self-contained HTML artifact. Send a FRAGMENT of plain HTML (headings, "
        "paragraphs, tables, inline SVG): rocky's page supplies the type, the colours and dark mode, so "
        "choose no colours or fonts yourself. Load the `artifact-design` skill before a substantial one. "
        "A full HTML document is kept exactly as written, for when the user asks for their own design. "
        "No external CDN/network references. Omit artifact_id to create a distinct work (even with the "
        "same title). To update, first inspect_artifact and pass its artifact_id and base_revision. "
        "Updates preserve version history in both static and live preview. Saving does not guarantee "
        "browser opening.",
        {"title": {"type": "string", "description": "Display title; not the identity."},
         "html": {"type": "string", "description": "A fragment (preferred) or a complete HTML document."},
         "artifact_id": {"type": "string", "description": "Existing stable ID to revise; omit for new."},
         "base_revision": {"type": "integer", "description": "Revision read before editing; prevents lost updates."},
         "theme": {"type": "string", "enum": list(THEMES), "description": "A fragment's look. Omit it: the user's default applies and a revision keeps its own. none = no colours of rocky's, for a fragment with its own CSS."},
         "layout": {"type": "string", "enum": list(LAYOUTS), "description": "document (reading width) or wide (dashboards, wide tables); a full document controls its own."}},
        ["title", "html"])
    inspect_schema = _fn_schema("inspect_artifact", "List project artifacts or read one revision and its history before editing.",
        {"artifact_id": {"type": "string", "description": "Omit to list project artifacts."},
         "revision": {"type": "integer", "description": "Omit for current revision."}}, [])
    return {"create_artifact": Tool(name="create_artifact", schema=schema, fn=create_artifact),
            "inspect_artifact": Tool(name="inspect_artifact", schema=inspect_schema, fn=inspect_artifact, risk="safe")}


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

    def __init__(self, workdir: Path, *, registry: ArtifactRegistry | None = None) -> None:
        self.workdir = workdir
        self.artifact_dir = workdir / ARTIFACT_DIR
        self.registry = registry
        self.port: int = 0
        # Unguessable per-session nonce gating every endpoint (see class doc).
        self.token: str = secrets.token_urlsafe(16)
        self._app: "web.Application | None" = None
        self._runner: "web.AppRunner | None" = None
        self._sse_queues: set["asyncio.Queue"] = set()
        self._start_lock = asyncio.Lock()

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

        async with self._start_lock:
            if self._runner is not None:
                return  # one server per session; repeated artifacts reuse it

            app = web.Application()
            app.router.add_get("/artifacts/{name}", self._serve_artifact)
            app.router.add_get("/list", self._list_artifacts)
            app.router.add_get("/api/events", self._handle_events)

            runner = web.AppRunner(app)
            await runner.setup()
            try:
                # Host: loopback. Docker: 0.0.0.0 is required (the published port
                # forwards to the container bridge, not its 127.0.0.1) — compose
                # binds the host side to 127.0.0.1, so it is never LAN-exposed.
                bind_host = "0.0.0.0" if os.environ.get("ROCKYCODE_IN_DOCKER") else "127.0.0.1"
                fixed_port = (
                    int(os.environ["ROCKYCODE_ARTIFACT_PORT"])
                    if "ROCKYCODE_ARTIFACT_PORT" in os.environ else 0
                )
                site = web.TCPSite(runner, bind_host, fixed_port)
                await site.start()
                for sock in site._server.sockets:
                    self.port = sock.getsockname()[1]
                    break
            except BaseException:
                await runner.cleanup()
                raise
            self._app = app
            self._runner = runner
            if self.registry is not None:
                self.registry.rebind_live(self.base_url, self.token)

    @property
    def base_url(self) -> str:
        return f"http://localhost:{self.port}"

    @property
    def is_running(self) -> bool:
        return self._runner is not None

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
        registry = self.registry or ArtifactRegistry(self.workdir)
        record = registry.get(name)
        fpath = record.path if record else self.artifact_dir / f"{safe}.html"
        if fpath.is_symlink() or not fpath.resolve().is_relative_to(self.artifact_dir.resolve()):
            raise web.HTTPNotFound(text="artifact not found")
        if not fpath.is_file():
            raise web.HTTPNotFound(text="artifact not found")
        doc = fpath.read_text(encoding="utf-8")
        events_url = f"{self.base_url}/api/events?t={self.token}&artifact={quote(name)}"
        return web.Response(text=_inject_reload(doc, events_url, name), content_type="text/html",
                            charset="utf-8", headers={"Cache-Control": "no-store"})

    async def _list_artifacts(self, request: "web.Request") -> "web.Response":
        from aiohttp import web

        self._require_token(request)
        registry = self.registry or ArtifactRegistry(self.workdir)
        return web.json_response([
            {"name": r.name, "artifact_id": r.artifact_id, "revision": r.revision,
             "size": r.path.stat().st_size,
             "url": f"{self.base_url}/artifacts/{quote(r.name)}?t={self.token}"}
            for r in registry.records()[:50] if r.path.is_file()
        ])

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
        tracked_name = unquote(request.query.get("artifact", ""))
        if (_safe_filename(tracked_name) != tracked_name
                or self.registry is None
                or self.registry.get(tracked_name) is None):
            tracked_name = ""
        if tracked_name:
            self.registry.set_client_connected(tracked_name, True)
        try:
            await resp.write(b"event: connected\ndata: {}\n\n")
            while True:
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_S)
                except asyncio.TimeoutError:
                    # Keep the SAME EventSource connection alive. Returning here
                    # made every browser reconnect every 30 seconds and left a
                    # window where a rebuild notification could be missed.
                    await resp.write(b": keepalive\n\n")
                    continue
                await resp.write(chunk.encode())
        except (ConnectionResetError, ConnectionError):
            pass
        finally:
            self._sse_queues.discard(queue)
            if tracked_name and self.registry is not None:
                self.registry.set_client_connected(tracked_name, False)
        return resp

    async def stop(self) -> None:
        async with self._start_lock:
            runner = self._runner
            self._runner = None
            self._app = None
            self.port = 0
            if runner is not None:
                await runner.cleanup()
