"""Headless exec mode: rocky as a subagent for OTHER coding agents.

`rockycode exec "task"` runs ONE task to completion with no human present,
under a strict machine contract (design: docs pending, see the exec command
help). It is the fifth consumer of Engine.run_turn(), after the TUI, bench,
serve, and goal mode.

  stdout   — JSONL only, one event per line. First line = `meta` (schema,
             session rk_ id, effective profile), last line = the `result`
             envelope. Anything human-shaped goes to stderr.
  approver — the caller can't answer prompts, so goal mode's command
             classifier decides: safe/moderate runs; an ask-tier action stops
             the run with a `blocked_on` grant token the caller can re-invoke
             with (exit 2 — the resume-with-grant loop); block-tier is refused
             UNCONDITIONALLY, no flag disables it, and the model must find a
             reversible path.
  exit     — 0 done | 1 error | 2 blocked on a grant | 3 step budget spent.

The calling agent is the supervisor: the envelope carries EVIDENCE (files
changed, commands run, refusals) — never verdicts — so the caller does its
own verification. Rocky never blocks waiting and never silently self-limits.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Callable, Optional

from rockycode.engine.events import (
    Compacted,
    EngineError,
    TextDelta,
    ThinkingDelta,
    ToolFinished,
    ToolStarted,
    TurnFinished,
    TurnStarted,
)
from rockycode.banner import fail, info
from rockycode.engine.loop import Engine
from rockycode.engine.safety import classify_command

SCHEMA = "rockyexec/1"

EXIT_DONE = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 2
EXIT_BUDGET = 3

# Headless-only ask tier: deletion. classify_command deliberately allows
# `rm -rf build/` because goal mode runs on an isolated copy — exec runs on
# the REAL working tree, so every delete needs an explicit `delete` grant.
_DELETE = re.compile(
    r"(?:^|[\s;&|(])(?:rm|rmdir|unlink)\b"
    r"|\bgit\s+clean\b"
    r"|\bfind\b[^\n|;]*\s-delete\b"
)

# Cap for string values inside emitted tool args (a write_file `content` can
# be an entire file; the caller only needs enough to see what happened).
_ARG_CLIP = 2000


class HeadlessApprover:
    """The approver for runs where the CALLER, not a human, supervises.

    Never waits: an ask-tier action records `blocked` (the exec driver stops
    the run and exits 2 so the caller can re-invoke with the grant) and a
    block-tier action records `refused` (the model gets the denial and keeps
    going). The grant token IS the safety pattern name, so `blocked_on.grant`
    tells the caller exactly what to pass to --allow.
    """

    def __init__(self, registry: dict, grants: frozenset[str] = frozenset()):
        self.registry = registry
        self.grants = grants
        self.blocked: Optional[dict] = None  # first ask-tier hit ends the run
        self.refused: list[dict] = []        # block-tier denials (evidence)

    async def __call__(self, name: str, args: dict) -> bool:
        if name == "bash":
            cmd = str(args.get("command", ""))
            v = classify_command(cmd)
            if v.action == "block":
                self.refused.append({"command": cmd, "reason": v.reason})
                return False
            if v.action == "ask" and v.pattern not in self.grants:
                self.blocked = {"grant": v.pattern, "reason": v.reason, "command": cmd}
                return False
            if _DELETE.search(cmd) and "delete" not in self.grants:
                self.blocked = {
                    "grant": "delete",
                    "reason": "deletes files on the real working tree",
                    "command": cmd,
                }
                return False
            return True
        # File tools are jailed to workdir + --allow-dir by the registry, so
        # safe/moderate tiers are exactly workspace-write. Anything unclassified
        # (dynamic MCP tools etc.) is gated like an ask.
        risk = getattr(self.registry.get(name), "risk", "risky")
        if risk in ("safe", "moderate"):
            return True
        # Honor the grant the docstring promises: "tool:<name>" tokens work
        # exactly like bash pattern grants (the routine envelope relies on it —
        # this path was unexercised until routines re-invoked with grants).
        if f"tool:{name}" in self.grants:
            return True
        self.blocked = {"grant": f"tool:{name}", "reason": f"unclassified risky tool '{name}'"}
        return False


def _scrub(text: str) -> str:
    """Redact secrets + the home path from anything crossing stdout. Unlike
    serve there is no debug bypass — the consumer is always another program."""
    try:
        from rockycode.engine.redact import redact
        text = redact(text)
    except Exception:  # noqa: BLE001 — best-effort
        pass
    try:
        home = str(Path.home())
        if home and home != "/":
            text = text.replace(home, "~")
    except Exception:  # noqa: BLE001
        pass
    return text


def _parse_args(ev_args: dict) -> dict:
    """ToolStarted carries the model's arguments UNPARSED as {'raw': json-str}
    (both loop.py branches yield before decoding). Decode for the caller;
    anything malformed passes through as-is."""
    raw = ev_args.get("raw")
    if isinstance(raw, str) and len(ev_args) == 1:
        try:
            parsed = json.loads(raw) if raw.strip() else {}
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return ev_args


def _clip_args(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > _ARG_CLIP:
            v = v[:_ARG_CLIP] + f"… [{len(v) - _ARG_CLIP} chars clipped]"
        out[k] = _scrub(v) if isinstance(v, str) else v
    return out


def event_to_line(ev, *, include_thinking: bool = False) -> Optional[dict]:
    """One engine Event → one JSONL dict, or None to skip. Thinking deltas are
    presentation-only and omitted by default — the engine's request history is
    a separate layer and never contains reasoning_content either way."""
    if isinstance(ev, TurnStarted):
        return {"type": "turn.started"}
    if isinstance(ev, (ThinkingDelta, TextDelta)):
        return None  # deltas are buffered by drive() — one event per block,
        #              not a 2023-style line per token
    if isinstance(ev, ToolStarted):
        return {"type": "tool.started", "call_id": ev.call_id, "tool": ev.tool,
                "args": _clip_args(_parse_args(ev.args))}
    if isinstance(ev, ToolFinished):
        return {"type": "tool.finished", "call_id": ev.call_id, "tool": ev.tool,
                "ok": ev.ok, "duration_s": round(ev.duration_s, 3),
                **_clip_output(_scrub(ev.output))}
    if isinstance(ev, Compacted):
        return {"type": "compacted", "strategy": ev.strategy}
    if isinstance(ev, TurnFinished):
        return {"type": "turn.finished", "steps": ev.steps, "usage": ev.usage}
    if isinstance(ev, EngineError):
        return {"type": "error", "message": _scrub(ev.message)}
    return None  # StateChanged / ContextReminder: pet-and-TUI noise


_OUTPUT_CAP = 2_000  # chars — the stream is a receipt; full output lives in the trajectory


def _clip_output(text: str) -> dict:
    if len(text) <= _OUTPUT_CAP:
        return {"output": text}
    return {"output": text[:_OUTPUT_CAP], "output_truncated": True,
            "output_chars": len(text)}


def _scrub_obj(obj):
    """Recursively _scrub every string in a dict/list (the result envelope's
    evidence fields — commands, refused, blocked_on — which the caller parses)."""
    if isinstance(obj, str):
        return _scrub(obj)
    if isinstance(obj, dict):
        return {k: _scrub_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_obj(v) for v in obj]
    return obj


def _stdout_line(obj: dict) -> None:
    """stdout carries ONLY event JSON — the purity contract callers parse by."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _caller_attribution(originator: str) -> dict:
    """Best-effort record of WHO invoked this run, for the trajectory's audit
    trail: a self-declared originator plus the parent process. Post-hoc
    attribution is the realistic defense for unattended abuse — see the
    exec design memory."""
    info: dict = {"originator": originator or os.getenv("ROCKYCODE_ORIGINATOR", "")}
    try:
        ppid = os.getppid()
        info["caller_pid"] = ppid
        name = subprocess.run(
            ["ps", "-p", str(ppid), "-o", "comm="],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        if name:
            info["caller_process"] = name
    except Exception:  # noqa: BLE001 — attribution must never break the run
        pass
    return info


def _final_text(history: list[dict]) -> str:
    """The last plain assistant message — the model's answer to the caller."""
    for m in reversed(history):
        if m.get("role") == "assistant" and m.get("content") and not m.get("tool_calls"):
            return str(m["content"])
    return ""


def build_exec_engine(
    *,
    model: str,
    workdir: Path,
    allowed_roots: tuple[Path, ...] = (),
    grants: frozenset[str] = frozenset(),
    max_steps: int = 30,
    originator: str = "",
    client=None,
    registry=None,
    sandbox_meta: Optional[dict] = None,
    extra_meta: Optional[dict] = None,
) -> tuple[Engine, HeadlessApprover]:
    """An Engine wired for headless exec: capped steps, headless approver,
    attribution in the trajectory meta. client/registry injection is for tests
    (fake DeepSeek stream, recording tools) — same seams bench uses.
    extra_meta lets a caller stamp identity onto the trajectory — the routine
    runner adds project_id + runner="routine" so the dream can grade runs."""
    engine = Engine(
        model=model,
        workdir=workdir,
        allowed_roots=allowed_roots,
        max_steps=max_steps,
        client=client,
        registry=registry,
        trajectory_meta={"headless_exec": True, **(sandbox_meta or {}),
                         **_caller_attribution(originator), **(extra_meta or {})},
    )
    engine._exec_sandbox_meta = sandbox_meta or {"sandbox": False, "network": False}
    approver = HeadlessApprover(engine.registry, grants)
    engine.approver = approver
    return engine, approver


async def drive(
    engine: Engine,
    approver: HeadlessApprover,
    prompt: str,
    *,
    write: Optional[Callable[[dict], None]] = None,
    include_thinking: bool = False,
    output_last_message: Optional[Path] = None,
) -> int:
    """Run one exec turn: meta line, event stream, result envelope, exit code."""
    from rockycode.session import public_id
    write = write or _stdout_line
    rk = public_id(engine.trajectory.session_id)

    write({
        "type": "meta",
        "schema": SCHEMA,
        "session": rk,
        "model": engine.model,
        "workdir": _scrub(str(engine.workdir)),
        # False warns the caller: overwrites here have no git safety net.
        "git": (engine.workdir / ".git").exists(),
        "profile": {
            "mode": "workspace-write",
            "grants": sorted(approver.grants),
            "max_steps": engine.max_steps,
            **getattr(engine, "_exec_sandbox_meta", {"sandbox": False, "network": False}),
        },
    })

    files_changed: list[str] = []
    commands: list[dict] = []
    started_cmds: dict[str, str] = {}  # call_id → bash command
    steps = 0
    usage: dict = {}
    budget_hit = False
    error_msg = ""

    text_buf: list[str] = []
    think_buf: list[str] = []

    def _flush_deltas() -> None:
        # one coherent event per block — flushed when something else happens
        if think_buf:
            write({"type": "thinking", "text": "".join(think_buf)})
            think_buf.clear()
        if text_buf:
            write({"type": "text", "text": "".join(text_buf)})
            text_buf.clear()

    gen = engine.run_turn(prompt)
    try:
        async for ev in gen:
            if isinstance(ev, TextDelta):
                text_buf.append(ev.text)
                continue
            if isinstance(ev, ThinkingDelta):
                if include_thinking:
                    think_buf.append(ev.text)
                continue
            if isinstance(ev, (ToolStarted, TurnFinished, EngineError)):
                _flush_deltas()
            if isinstance(ev, ToolStarted):
                args = _parse_args(ev.args)
                if ev.tool == "bash":
                    started_cmds[ev.call_id] = str(args.get("command", ""))
                elif ev.tool in ("write_file", "edit_file") and args.get("path"):
                    p = str(args["path"])
                    if p not in files_changed:
                        files_changed.append(p)
            elif isinstance(ev, ToolFinished) and ev.call_id in started_cmds:
                commands.append({"command": started_cmds.pop(ev.call_id), "ok": ev.ok})
            elif isinstance(ev, TurnFinished):
                steps, usage = ev.steps, ev.usage
            elif isinstance(ev, EngineError):
                # Coupling: loop.py words its step-cap error "step limit …" —
                # that's a budget stop (resumable checkpoint), not a failure.
                if ev.message.startswith("step limit"):
                    budget_hit = True
                else:
                    error_msg = ev.message
            line = event_to_line(ev, include_thinking=include_thinking)
            if line:
                write(line)
            if approver.blocked:
                break  # fail fast: the caller decides, then resumes with a grant
    except Exception as e:  # noqa: BLE001 — envelope + exit 1, never a stdout traceback
        error_msg = f"{type(e).__name__}: {e}"
        print(traceback.format_exc(), file=sys.stderr, flush=True)
    finally:
        # Run the engine's cleanup (history repair + trajectory stubs) NOW —
        # a broken-out async-for otherwise defers it to GC.
        await gen.aclose()
    _flush_deltas()  # anything still buffered (blocked break / early end)

    if approver.blocked:
        status, code = "blocked", EXIT_BLOCKED
    elif error_msg:
        status, code = "error", EXIT_ERROR
    elif budget_hit:
        status, code = "budget", EXIT_BUDGET
    else:
        status, code = "done", EXIT_DONE

    summary = _scrub(_final_text(engine.history))
    # F5: the caller PARSES these fields, so they get the same scrub as the
    # event stream — a token embedded in a command URL must not leak here.
    result = {
        "type": "result",
        "status": status,
        "session": rk,
        "summary": summary,
        "blocked_on": _scrub_obj(approver.blocked),
        "evidence": {
            "files_changed": [_scrub(p) for p in files_changed],
            "commands": _scrub_obj(commands),
            "refused": _scrub_obj(approver.refused),
        },
        "steps": steps,
        "usage": usage,
    }
    if error_msg:
        result["error"] = _scrub(error_msg)
    write(result)
    engine.trajectory.outcome({k: v for k, v in result.items() if k != "type"})
    # Heuristic reward line LAST (self-evolve): readers take the last outcome
    # record and the dream's judge gate keys on source="heuristic" — this is
    # what makes exec/routine runs gradeable. The envelope above stays in the
    # file as evidence.
    engine.finalize_outcome()

    if output_last_message is not None:
        try:
            output_last_message.write_text(summary, encoding="utf-8")
        except OSError as e:
            print(f"could not write --output-last-message: {e}", file=sys.stderr)

    return code


async def run_exec(
    *,
    prompt: str,
    model: str,
    workdir: Path,
    allowed_roots: tuple[Path, ...] = (),
    grants: frozenset[str] = frozenset(),
    max_steps: int = 30,
    originator: str = "",
    include_thinking: bool = False,
    output_last_message: Optional[Path] = None,
    write: Optional[Callable[[dict], None]] = None,
    client=None,
    registry=None,
    sandbox: bool = True,
    network: bool = False,
    err=None,
    extra_meta: Optional[dict] = None,
) -> int:
    """Provision the sandbox (default), build the engine, drive one task.

    The task can originate from an untrusted source (an issue, a page a
    delegating agent read), so by default every tool runs inside a Docker
    container with NO network: a `rm -rf /` hits the container's root, home
    secrets aren't mounted, and there's no egress to exfiltrate over. The
    command classifier stays on top as defense-in-depth, its real designed
    role. --no-sandbox is the explicit, loud host escape hatch.
    """
    sb = None
    sandbox_meta = {"sandbox": False, "network": False}
    if sandbox and registry is None:  # registry injected → tests, already wired
        try:
            from rockycode.engine.sandbox import ChatSandbox, build_sandbox_registry
            sb = await ChatSandbox.start(workdir, network=network)
            registry = build_sandbox_registry(sb)
            sandbox_meta = {"sandbox": True, "network": bool(network),
                            "container": sb.container_id[:12]}
            if err is not None:
                err.print(f"[dim]· sandbox: on (container {sb.container_id[:12]}…) · "
                          f"network {'on' if network else 'off'}[/]")
        except Exception as e:  # noqa: BLE001 — Docker missing/broken: fail clearly
            if err is not None:
                fail(err, f"exec needs Docker for the sandbox — {type(e).__name__}: {e}")
                info(err, "start Docker Desktop, or pass --no-sandbox to run on the "
                          "host (UNSAFE for untrusted input).")
            return EXIT_ERROR
    elif not sandbox and registry is None and err is not None:
        err.print("[bold yellow]⚠ --no-sandbox: tools run on the HOST with no "
                  "isolation. The command classifier is a denylist, not a security "
                  "boundary — only use with input you fully trust.[/]")

    try:
        engine, approver = build_exec_engine(
            model=model, workdir=workdir, allowed_roots=allowed_roots, grants=grants,
            max_steps=max_steps, originator=originator, client=client, registry=registry,
            sandbox_meta=sandbox_meta, extra_meta=extra_meta,
        )
        return await drive(
            engine, approver, prompt,
            write=write, include_thinking=include_thinking,
            output_last_message=output_last_message,
        )
    finally:
        if sb is not None:
            try:
                await sb.stop()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
