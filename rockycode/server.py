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
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

from rockycode.engine import Engine
from rockycode.engine.delivery import TurnDelivery
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
            "args": ev.args, "envelope": asdict(ev.envelope),
        })
    if isinstance(ev, ToolFinished):
        return _notify("session/tool_finished", {
            "session_id": session_id, "call_id": ev.call_id, "tool": ev.tool,
            "output": ev.output, "ok": ev.ok, "duration_s": ev.duration_s,
            "envelope": asdict(ev.envelope),
        })
    if isinstance(ev, Compacted):
        return _notify("session/compacted", {
            "session_id": session_id, "strategy": ev.strategy,
            "tokens_before": ev.tokens_before, "tokens_after": ev.tokens_after,
        })
    if isinstance(ev, TurnFinished):
        return _notify("session/turn_finished", {
            "session_id": session_id, "steps": ev.steps, "usage": ev.usage,
            "turn": ev.turn,
        })
    if isinstance(ev, EngineError):
        return _notify("session/error", {
            "session_id": session_id, "message": _scrub_message(ev.message),
            **({"kind": ev.kind, "detail": _scrub_message(ev.detail)} if ev.kind else {}),
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
        self.delivery: TurnDelivery | None = None
        self.created_at = datetime.now(timezone.utc).isoformat()

    def cancel(self) -> None:
        task = self._task
        if self.delivery is not None:
            self.delivery.paused = True
            if self.delivery.running:
                task = self.delivery.task
            self.delivery.on_change()
        if task is not None and not task.done():
            task.cancel()  # The dispatcher and handle_chat share this task.

    @property
    def running(self) -> bool:
        return bool(self.delivery and self.delivery.running) or (
            self._task is not None and not self._task.done())


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
        engine.enable_planning()
        engine.approver = self._make_approver(sid, engine)
        # Evolve v2: serve sessions reflect like chat ones (checkpoints ride
        # turn boundaries; the final reflect runs at shutdown below).
        try:
            from rockycode.dream.reflect import attach_from_config
            attach_from_config(engine, self._workdir)
        except Exception:  # noqa: BLE001 — evolve must never block a session
            pass

        # Wire up artifact support: create_artifact tool + live server
        try:
            from rockycode.engine.artifact import ArtifactServer, build_artifact_tools
            tools = build_artifact_tools(workdir=self._workdir, engine=engine)
            engine.registry.update(tools)
            registry = engine.artifact_registry
            server = ArtifactServer(self._workdir, registry=registry)

            def _artifact_changed(action, record):
                _write_line(_notify("session/artifact_changed", {
                    "session_id": sid,
                    "action": action,
                    "artifact": record.as_dict(),
                    "server_running": server.is_running,
                    "server_url": server.base_url if server.is_running else None,
                }))

            registry.subscribe(_artifact_changed)

            async def _artifact_target():
                await server.start()
                return server
            engine.artifact_target = _artifact_target
            engine.artifact_server = server
        except Exception:
            pass  # artifacts are optional

        sess = Session(sid, engine)
        self._sessions[sid] = sess

        async def run(item):
            return await self.handle_chat(sid, item.prompt)

        def changed():
            _write_line(_notify("session/queue_changed", {
                "session_id": sid, **sess.delivery.snapshot(),
            }))

        sess.delivery = TurnDelivery(run, on_change=changed)
        return sess

    def _make_approver(self, session_id: str, engine):
        """The shared approval pipeline (engine/approval.py) with the editor
        client as the human: a would-ask call becomes a session/request_permission
        notification and waits for session/permission_response (120 s → deny);
        a decision taken without asking is a session/approval_note notification.
        Static policy, session grants and the verifier run here exactly as in
        the TUI; the mode comes from config (`permission`, default ask)."""
        from rockycode.engine.approval import ToolApprover
        try:
            from rockycode.config import load as _load_config
            mode = str(_load_config(self._workdir).get("permission", "ask") or "ask")
        except Exception:  # noqa: BLE001 — config trouble must not block a session
            mode = "ask"

        async def ask(req) -> str:
            eid = uuid.uuid4().hex[:8]
            ev = asyncio.Event()
            self._perm_events[eid] = ev
            self._perm_sessions[eid] = session_id
            _write_line(_notify("session/request_permission", {
                "session_id": session_id, "event_id": eid,
                "tool": req.tool, "args": req.args, "risk": req.risk,
                "detail": req.detail, "warning": req.warning,
            }))
            try:
                await asyncio.wait_for(ev.wait(), timeout=120.0)
            except asyncio.TimeoutError:
                self._cleanup_perm(eid)
                return "deny"
            result = self._perm_results.pop(eid, False)
            self._cleanup_perm(eid)
            return "once" if result else "deny"

        async def notify(kind: str, **info) -> None:
            # What the pipeline decided without asking — a static block, or the
            # verifier's auto allow/deny — so the client can say so in its UI.
            _write_line(_notify("session/approval_note", {
                "session_id": session_id, "kind": kind,
                "tool": info.get("tool"), "detail": info.get("detail"),
                "error": info.get("error"),
            }))

        return ToolApprover(registry=engine.registry, workdir=self._workdir, ask=ask, notify=notify,
                            mode=mode if mode in ("yolo", "ask", "careful") else "ask", engine=engine)

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

    async def handle_chat(self, session_id: str, message: str) -> bool:
        sess = self.get(session_id)
        if sess is None:
            return False
        sess._task = asyncio.current_task()
        completed = True
        try:
            async for ev in sess.engine.run_turn(message):
                if isinstance(ev, EngineError) and not ev.message.startswith("compaction summary failed"):
                    completed = False
                n = _event_to_notification(session_id, ev)
                if n is not None:
                    _write_line(n)
        except asyncio.CancelledError:
            completed = False  # _repair_history runs in run_turn's finally
        except Exception:
            completed = False
            _write_line(_notify("session/error", {
                "session_id": session_id,
                "message": _client_error(),
            }))
        finally:
            sess._task = None
            if sess.engine.planning.path is not None:
                _write_line(_notify("session/plan_changed", {
                    "session_id": session_id,
                    **sess.engine.planning.snapshot(include_text=True),
                }))
        return completed

    def plan_action(self, session_id: str, action: str, *, revision: str = "") -> tuple[dict, str | None]:
        """Host actions shared with TUI semantics; only approval returns a build turn."""
        from rockycode.engine.planning import ENTRY_MESSAGE

        sess = self.get(session_id)
        if sess is None:
            raise ValueError("unknown session")
        if sess.running and action != "status":
            raise ValueError("finish or cancel the current turn before changing plan state")
        planning = sess.engine.planning
        message = None
        if action == "start":
            was_active = planning.active
            planning.start()
            if not was_active and any(m.get("role") == "user" for m in sess.engine.history):
                message = ENTRY_MESSAGE
        elif action == "review":
            planning.request_review()
        elif action == "discuss":
            planning.continue_discussion()
        elif action == "approve":
            message = planning.approve(revision)
        elif action == "leave":
            planning.leave()
        elif action != "status":
            raise ValueError("unknown plan action")
        return {"session_id": session_id, **planning.snapshot(include_text=True)}, message

    def list_sessions(self) -> list[dict]:
        return [
            {
                "session_id": s.session_id,
                "created_at": s.created_at,
                "n_messages": len(s.engine.history),
                "running": s.running,
                "delivery": s.delivery.snapshot() if s.delivery else None,
                "artifact_count": getattr(
                    getattr(s.engine, "artifact_registry", None), "count", 0),
                "artifact_server_running": bool(getattr(
                    getattr(s.engine, "artifact_server", None), "is_running", False)),
            }
            for s in self._sessions.values()
        ]

    def artifact_status(self, session_id: str) -> dict:
        sess = self.get(session_id)
        registry = getattr(sess.engine, "artifact_registry", None) if sess else None
        server = getattr(sess.engine, "artifact_server", None) if sess else None
        running = bool(getattr(server, "is_running", False))
        return {
            "session_id": session_id,
            "server_running": running,
            "server_url": server.base_url if running else None,
            "artifacts": registry.snapshot("session") if registry is not None else [],
        }

    async def stop_artifacts(self, session_id: str) -> bool:
        sess = self.get(session_id)
        server = getattr(sess.engine, "artifact_server", None) if sess else None
        if server is None or not server.is_running:
            return False
        await server.stop()
        return True

    def cancel_session(self, session_id: str) -> bool:
        sess = self.get(session_id)
        if sess is None:
            return False
        sess.cancel()
        return True

    async def shutdown(self) -> None:
        await asyncio.gather(*(s.delivery.close() for s in self._sessions.values() if s.delivery))
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
            server = getattr(sess.engine, "artifact_server", None)
            if server is not None:
                try:
                    await server.stop()
                except Exception:  # noqa: BLE001 — best-effort at teardown
                    pass
            try:
                sess.engine.finalize_outcome()
                await sess.engine.run_final_hooks()
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
    max_tokens: int | None = None,      # None = the model's registry cap
    context_window: int | None = None,  # None = the model's registry window
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
                import rockycode
                from rockycode.onboarding import is_configured
                _write_line(_response(msg_id, {
                    "version": rockycode.__version__,
                    "session_id": sess.session_id,
                    "model": model,
                    "configured": is_configured(),
                }))

            elif method == "chat":
                sid = params.get("session_id", "")
                if not sid or sid not in mgr._sessions:
                    sess = mgr._new_session()
                    sid = sess.session_id
                try:
                    receipt = await mgr.get(sid).delivery.submit(
                        params.get("message", ""), mode=params.get("delivery", "interrupt"))
                except ValueError as exc:
                    _write_line(_error(msg_id, -32602, str(exc)))
                    continue
                _write_line(_response(msg_id, {"session_id": sid, **receipt}))

            elif method in {"queue/status", "queue/resume", "queue/remove", "queue/clear"}:
                sid = params.get("session_id", "")
                sess = mgr.get(sid)
                if sess is None:
                    _write_line(_error(msg_id, -32602, "unknown session"))
                    continue
                if method == "queue/resume":
                    await sess.delivery.resume()
                elif method == "queue/remove":
                    await sess.delivery.remove(params.get("message_id"))
                elif method == "queue/clear":
                    await sess.delivery.clear()
                _write_line(_response(msg_id, {"session_id": sid, **sess.delivery.snapshot()}))

            elif method == "cancel":
                sid = params.get("session_id", "")
                ok = mgr.cancel_session(sid)
                _write_line(_response(msg_id, {"cancelled": ok}))

            elif method in {"plan/start", "plan/status", "plan/review", "plan/discuss",
                            "plan/approve", "plan/leave"}:
                sid = params.get("session_id", "")
                try:
                    state, message = mgr.plan_action(sid, method.split("/")[1],
                                                     revision=params.get("revision", ""))
                except ValueError as exc:
                    _write_line(_error(msg_id, -32602, str(exc)))
                    continue
                if message is not None:
                    await mgr.get(sid).delivery.submit(message, mode="interrupt")
                _write_line(_response(msg_id, state))

            elif method == "list_sessions":
                _write_line(_response(msg_id, {"sessions": mgr.list_sessions()}))

            elif method == "get_status":
                sid = params.get("session_id", "")
                sess = mgr.get(sid)
                _write_line(_response(msg_id, {
                    "session_id": sid,
                    "state": "busy" if (sess and sess.running) else "idle",
                    "planning": sess.engine.planning.snapshot() if sess else None,
                    "delivery": sess.delivery.snapshot() if sess else None,
                }))

            elif method == "artifact/list":
                sid = params.get("session_id", "")
                _write_line(_response(msg_id, mgr.artifact_status(sid)))

            elif method == "artifact/stop":
                sid = params.get("session_id", "")
                stopped = await mgr.stop_artifacts(sid)
                _write_line(_response(msg_id, {
                    **mgr.artifact_status(sid),
                    "stopped": stopped,
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
