"""JSON-RPC 2.0 server over stdin/stdout — wraps the Engine for editor clients.

One line per JSON message (NDJSON, same transport as LSP). The server manages
multiple sessions; each session has its own Engine instance and its own
run_turn task. Events stream out as notifications; permission requests become
blocking request/response pairs.

This is the backend the VS Code extension connects through.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

from rockycode.engine import Engine
from rockycode.engine.events import (
    Compacted,
    EngineError,
    StateChanged,
    TextDelta,
    ThinkingDelta,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)

# ─────────────────────────────────────────────────────────────────────────────
# protocol helpers
# ─────────────────────────────────────────────────────────────────────────────


def _notify(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "method": method, "params": params}


def _response(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code: int, message: str):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


_DEBUG = os.getenv("ROCKYCODE_SERVE_DEBUG") == "1"


def _client_error() -> str:
    """A message safe to send to the client. The full traceback goes to stderr
    for the operator; the client sees a generic string unless
    ROCKYCODE_SERVE_DEBUG=1 — so a hostile client can't harvest local paths /
    internals from an error over the wire. Call only from inside an `except`."""
    tb = traceback.format_exc()
    print(tb, file=sys.stderr, flush=True)
    return tb if _DEBUG else "internal error"


def _scrub_message(msg: str) -> str:
    """Client-safe error text for streamed session/error events: redact secrets
    and the user's home path so a hostile editor client can't harvest local
    paths / internals (parity with _client_error). Full text under
    ROCKYCODE_SERVE_DEBUG=1."""
    if _DEBUG:
        return msg
    try:
        from rockycode.engine.redact import redact
        msg = redact(msg)
    except Exception:  # noqa: BLE001 — best-effort
        pass
    try:
        home = str(Path.home())
        if home and home != "/":
            msg = msg.replace(home, "~")
    except Exception:  # noqa: BLE001
        pass
    return msg


def _event_to_notification(session_id: str, ev) -> Optional[dict]:
    """Convert an engine Event to a JSON-RPC notification dict, or None to skip."""
    if isinstance(ev, StateChanged):
        return _notify("session/state_changed", {
            "session_id": session_id, "state": ev.state.value,
        })
    if isinstance(ev, ThinkingDelta):
        return _notify("session/thinking_delta", {
            "session_id": session_id, "text": ev.text,
        })
    if isinstance(ev, TextDelta):
        return _notify("session/text_delta", {
            "session_id": session_id, "text": ev.text,
        })
    if isinstance(ev, ToolStarted):
        return _notify("session/tool_started", {
            "session_id": session_id, "call_id": ev.call_id, "tool": ev.tool,
            "args": ev.args,
        })
    if isinstance(ev, ToolFinished):
        return _notify("session/tool_finished", {
            "session_id": session_id, "call_id": ev.call_id, "tool": ev.tool,
            "output": ev.output, "ok": ev.ok, "duration_s": ev.duration_s,
        })
    if isinstance(ev, Compacted):
        return _notify("session/compacted", {
            "session_id": session_id, "strategy": ev.strategy,
            "tokens_before": ev.tokens_before, "tokens_after": ev.tokens_after,
        })
    if isinstance(ev, TurnFinished):
        return _notify("session/turn_finished", {
            "session_id": session_id, "steps": ev.steps, "usage": ev.usage,
        })
    if isinstance(ev, EngineError):
        return _notify("session/error", {
            "session_id": session_id, "message": _scrub_message(ev.message),
        })
    return None


# ─────────────────────────────────────────────────────────────────────────────
# output — thread-safe single-line JSON to stdout
# ─────────────────────────────────────────────────────────────────────────────

def _write_line(msg: dict) -> None:
    """Write one JSON-RPC message line to stdout. NOT async — called from both
    the main server loop and session tasks. The write+flush has no await between
    them, so within one event loop two coroutines can't interleave bytes. stdout
    carries ONLY protocol JSON; logs and errors go to stderr."""
    line = json.dumps(msg, ensure_ascii=False, default=str) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()


# ─────────────────────────────────────────────────────────────────────────────
# session
# ─────────────────────────────────────────────────────────────────────────────


class Session:
    def __init__(self, session_id: str, engine: Engine):
        self.session_id = session_id
        self.engine = engine
        self._task: Optional[asyncio.Task] = None
        self.created_at = datetime.now(timezone.utc).isoformat()

    def cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()


# ─────────────────────────────────────────────────────────────────────────────
# session manager
# ─────────────────────────────────────────────────────────────────────────────


class SessionManager:
    def __init__(self, model: str, workdir: Path, **engine_kwargs):
        self._model = model
        self._workdir = workdir
        self._engine_kwargs = engine_kwargs
        self._system_prompt = engine_kwargs.pop("system_prompt", "") or ""
        self._sessions: dict[str, Session] = {}
        # Permission bridge: engine approver → JSON-RPC notification → wait
        # for client's session/permission_response.
        self._perm_events: dict[str, asyncio.Event] = {}
        self._perm_results: dict[str, bool] = {}
        self._perm_sessions: dict[str, str] = {}  # event_id → session_id

    def _new_session(self) -> Session:
        sid = uuid.uuid4().hex[:12]
        kwargs = dict(self._engine_kwargs)
        if self._system_prompt:
            kwargs["system_prompt"] = self._system_prompt
        engine = Engine(model=self._model, workdir=self._workdir, **kwargs)

        # Wire up artifact support: create_artifact tool + live server
        try:
            from rockycode.engine.artifact import ArtifactServer, build_artifact_tools
            tools = build_artifact_tools(workdir=self._workdir, engine=engine)
            engine.registry.update(tools)
            server = ArtifactServer(self._workdir)
            async def _artifact_target():
                await server.start()
                return server
            engine.artifact_target = _artifact_target
        except Exception:
            pass  # artifacts are optional

        sess = Session(sid, engine)
        self._sessions[sid] = sess
        return sess

    def _make_approver(self, session_id: str):
        """Return an async approve(name, args) → bool that bridges to the
        JSON-RPC client via a notification + wait."""

        async def approve(name: str, args: dict) -> bool:
            eid = uuid.uuid4().hex[:8]
            ev = asyncio.Event()
            self._perm_events[eid] = ev
            self._perm_sessions[eid] = session_id
            risk = getattr(
                self._sessions[session_id].engine.registry.get(name), "risk", "risky"
            )
            _write_line(_notify("session/request_permission", {
                "session_id": session_id, "event_id": eid,
                "tool": name, "args": args, "risk": risk,
            }))
            try:
                await asyncio.wait_for(ev.wait(), timeout=120.0)
            except asyncio.TimeoutError:
                self._cleanup_perm(eid)
                return False
            result = self._perm_results.pop(eid, False)
            self._cleanup_perm(eid)
            return result

        return approve

    def resolve_permission(self, event_id: str, allowed: bool) -> bool:
        if event_id in self._perm_events:
            self._perm_results[event_id] = allowed
            self._perm_events[event_id].set()
            return True
        return False

    def _cleanup_perm(self, eid: str) -> None:
        self._perm_events.pop(eid, None)
        self._perm_sessions.pop(eid, None)

    def get(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    async def handle_chat(self, session_id: str, message: str) -> None:
        sess = self.get(session_id)
        if sess is None:
            return
        sess.engine.approver = self._make_approver(session_id)
        sess._task = asyncio.current_task()
        try:
            async for ev in sess.engine.run_turn(message):
                n = _event_to_notification(session_id, ev)
                if n is not None:
                    _write_line(n)
        except asyncio.CancelledError:
            pass  # _repair_history runs in run_turn's finally
        except Exception:
            _write_line(_notify("session/error", {
                "session_id": session_id,
                "message": _client_error(),
            }))
        finally:
            sess._task = None

    def list_sessions(self) -> list[dict]:
        return [
            {
                "session_id": s.session_id,
                "created_at": s.created_at,
                "n_messages": len(s.engine.history),
                "running": s.running,
            }
            for s in self._sessions.values()
        ]

    def cancel_session(self, session_id: str) -> bool:
        sess = self.get(session_id)
        if sess is None:
            return False
        sess.cancel()
        return True

    async def shutdown(self) -> None:
        tasks = []
        for sess in self._sessions.values():
            sess.cancel()
            if sess._task is not None:
                tasks.append(sess._task)
        # Await the cancellations rather than sleeping — otherwise the tasks are
        # still pending at interpreter exit ("Task was destroyed but it is
        # pending" noise on stderr).
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:  # a dying session must not block shutdown
                pass
        # Reward lines for the dream (self-evolve): flush each session's
        # heuristic outcome now that its turns are done. Idempotent, and a
        # no-turn session writes nothing.
        for sess in self._sessions.values():
            try:
                sess.engine.finalize_outcome()
            except Exception:  # noqa: BLE001 — best-effort at teardown
                pass


# ─────────────────────────────────────────────────────────────────────────────
# server loop
# ─────────────────────────────────────────────────────────────────────────────


async def _read_messages() -> AsyncIterator[dict]:
    """Read NDJSON messages from stdin using a thread so we don't block the
    event loop on synchronous stdin reads."""
    loop = asyncio.get_running_loop()

    def _read() -> str:
        return sys.stdin.readline()

    while True:
        line = await loop.run_in_executor(None, _read)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        # A hostile/buggy client can send valid JSON that isn't an object
        # (42, "x", null, []). Drop it here so the dispatch loop only ever sees
        # dicts — one bad line must not crash the server on msg.get(...).
        if isinstance(msg, dict):
            yield msg


async def run_server(
    model: str,
    workdir: Path,
    thinking: bool = True,
    reasoning_effort: str = "max",
    max_tokens: int = 16384,
    context_window: int = 131072,
    max_steps: int = 0,
    system_prompt: str = "",
) -> None:
    """Blocking: reads JSON-RPC from stdin, writes to stdout, runs until EOF."""

    mgr = SessionManager(
        model=model, workdir=workdir,
        thinking=thinking, reasoning_effort=reasoning_effort,
        max_tokens=max_tokens, context_window=context_window,
        max_steps=max_steps, system_prompt=system_prompt,
    )

    chat_tasks: dict[str, asyncio.Task] = {}

    async for msg in _read_messages():
        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params") or {}   # tolerate a missing or null "params"

        try:
            if method == "initialize":
                sess = mgr._new_session()
                # Report whether a usable key is present (rocky's credential
                # chain: env, .env files serve loaded, keychain) so the client's
                # "setup required" card reflects reality instead of guessing
                # from the editor's own environment.
                from rockycode.onboarding import is_configured
                _write_line(_response(msg_id, {
                    "version": "0.1.0",
                    "session_id": sess.session_id,
                    "model": model,
                    "configured": is_configured(),
                }))

            elif method == "chat":
                sid = params.get("session_id", "")
                if not sid or sid not in mgr._sessions:
                    sess = mgr._new_session()
                    sid = sess.session_id
                # Cancel any running turn on this session before starting a new one
                prev = chat_tasks.get(sid)
                if prev and not prev.done():
                    mgr.cancel_session(sid)
                    prev.cancel()
                    try:
                        await prev
                    except asyncio.CancelledError:
                        pass
                task = asyncio.create_task(mgr.handle_chat(sid, params.get("message", "")))
                chat_tasks[sid] = task
                _write_line(_response(msg_id, {"session_id": sid}))

            elif method == "cancel":
                sid = params.get("session_id", "")
                ok = mgr.cancel_session(sid)
                _write_line(_response(msg_id, {"cancelled": ok}))

            elif method == "list_sessions":
                _write_line(_response(msg_id, {"sessions": mgr.list_sessions()}))

            elif method == "get_status":
                sid = params.get("session_id", "")
                sess = mgr.get(sid)
                _write_line(_response(msg_id, {
                    "session_id": sid,
                    "state": "busy" if (sess and sess.running) else "idle",
                }))

            elif method == "session/permission_response":
                eid = params.get("event_id", "")
                allowed = params.get("allowed", False)
                mgr.resolve_permission(eid, allowed)
                _write_line(_response(msg_id, {"ack": True}))

            elif method == "shutdown":
                _write_line(_response(msg_id, {"ok": True}))
                break

            else:
                _write_line(_error(msg_id, -32601, f"unknown method: {method}"))

        except Exception:
            if msg_id is not None:
                _write_line(_error(msg_id, -32603, _client_error()))

    await mgr.shutdown()   # cancel any in-flight turns on EOF / shutdown
