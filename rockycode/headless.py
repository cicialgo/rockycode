"""Headless exec mode: rocky as a subagent for OTHER coding agents.

`rockycode exec "task"` runs ONE task to completion with no human present,
under a strict machine contract (design: docs pending, see the exec command
help). It is the fifth consumer of Engine.run_turn(), after the TUI, bench,
serve, and goal mode.

  stdout   — JSONL only, one event per line. First line = `meta` (schema,
             session rk_ id, effective profile), last line = the `result`
             envelope. Anything human-shaped goes to stderr. By default only
             meta → text → result cross stdout (the caller's context is the
             scarce resource); `events=True` adds tool.*/turn.* lines.
  profile  — read: read_file/grep/glob/view_image only, no shell, no writes,
             no Docker · write: + write_file/edit_file jailed to the workdir,
             still no shell, no Docker · full: + bash, Docker-sandboxed by
             default. Most agent-to-agent delegation is read or write.
  approver — the caller can't answer prompts, so goal mode's command
             classifier decides: safe/moderate runs; an ask-tier action stops
             the run with a `blocked_on` grant token (exit 2). --resume carries
             history into a NEW session with an explicit follow-up prompt;
             --allow grants apply only to that invocation. No automatic replay
             or exactly-once execution is promised. Block-tier is refused
             UNCONDITIONALLY, no flag disables it, and the model must find a
             reversible path.
  exit     — 0 done | 1 error | 2 blocked on a grant | 3 step budget spent.

The calling agent is the supervisor: the envelope carries EVIDENCE (files
changed, commands run, refusals) — never verdicts — so the caller does its
own verification. Rocky never blocks waiting and never silently self-limits.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
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
from rockycode.palette import AMBER
from rockycode.engine.approval import HeadlessApprover
from rockycode.engine.loop import Engine

SCHEMA = "rockyexec/1"

PROFILES = ("read", "write", "full")
# Host tools a non-full profile may use. File reads/writes are bounded to
# workdir + --allow-dir; check_code (project-controlled execution) stays out.
# view_image may use a trusted user-configured backend, never a project CLI.
# This tool list is not process isolation: hosted deployments must run the
# entire Engine, clients and extensions inside a tenant/task sandbox.
PROFILE_TOOLS = {
    "read": {"read_file", "grep", "glob", "view_image"},
    "write": {"read_file", "grep", "glob", "view_image", "write_file", "edit_file"},
}
PROFILE_MODE = {"read": "read-only", "write": "workspace-write (no shell)",
                "full": "workspace-write"}
# Stream lines that only a caller asking for the full receipt wants.
_EVENT_TYPES = {"turn.started", "turn.finished", "tool.started", "tool.finished",
                "compacted", "thinking"}

EXIT_DONE = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 2
EXIT_BUDGET = 3

# Cap for string values inside emitted tool args (a write_file `content` can
# be an entire file; the caller only needs enough to see what happened).
_ARG_CLIP = 2000


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
    a separate layer whose reasoning fields follow the active model protocol."""
    if isinstance(ev, TurnStarted):
        return {"type": "turn.started", "turn": ev.turn}
    if isinstance(ev, (ThinkingDelta, TextDelta)):
        return None  # deltas are buffered by drive() — one event per block,
        #              not a 2023-style line per token
    if isinstance(ev, ToolStarted):
        return {"type": "tool.started", "call_id": ev.call_id, "tool": ev.tool,
                "args": _clip_args(_parse_args(ev.args)), "envelope": asdict(ev.envelope)}
    if isinstance(ev, ToolFinished):
        return {"type": "tool.finished", "call_id": ev.call_id, "tool": ev.tool,
                "ok": ev.ok, "duration_s": round(ev.duration_s, 3),
                "envelope": asdict(ev.envelope), **_clip_output(_scrub(ev.output))}
    if isinstance(ev, Compacted):
        return {"type": "compacted", "strategy": ev.strategy}
    if isinstance(ev, TurnFinished):
        return {"type": "turn.finished", "steps": ev.steps, "usage": ev.usage, "turn": ev.turn}
    if isinstance(ev, EngineError):
        # `kind` (auth, rate_limit, network, … — engine/errors.py) is present for a
        # failed model request, so a caller can branch without parsing the sentence.
        return {"type": "error", "message": _scrub(ev.message), **({"kind": ev.kind} if ev.kind else {})}
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


def host_profile_registry(workdir: Path, allowed_roots: tuple[Path, ...] = (),
                          profile: str = "read") -> dict:
    """The host registry trimmed to a read/write profile's tool set."""
    from rockycode.engine.tools import build_registry
    keep = PROFILE_TOOLS[profile]
    return {n: t for n, t in build_registry(workdir, allowed_roots).items() if n in keep}


def build_exec_engine(
    *,
    model: str,
    workdir: Path,
    allowed_roots: tuple[Path, ...] = (),
    grants: frozenset[str] = frozenset(),
    max_steps: int = 30,
    originator: str = "",
    client=None,
    model_choice=None,
    registry=None,
    sandbox_meta: Optional[dict] = None,
    extra_meta: Optional[dict] = None,
    profile: str = "full",
    max_tokens: Optional[int] = None,
    reasoning_effort: Optional[str] = None,
) -> tuple[Engine, HeadlessApprover]:
    """An Engine wired for headless exec: capped steps, headless approver,
    attribution in the trajectory meta. client/registry injection is for tests
    (fake DeepSeek stream, recording tools) — same seams bench uses.
    extra_meta lets a caller stamp identity onto the trajectory — the routine
    runner adds project_id + runner="routine" so the dream can grade runs."""
    from rockycode.session import get_project
    project = get_project(workdir)
    engine = Engine(
        model=model,
        workdir=workdir,
        allowed_roots=allowed_roots,
        max_steps=max_steps,
        max_tokens=max_tokens,
        thinking=reasoning_effort != "off",
        reasoning_effort=reasoning_effort if reasoning_effort not in (None, "off") else "max",
        finalize_steps=3,  # preserve the headless/routine envelope independently of chat/bench defaults
        client=client,
        model_choice=model_choice,
        registry=registry,
        trajectory_meta={"headless_exec": True, **(sandbox_meta or {}),
                         "project_id": project.id, "project_name": project.name,
                         **_caller_attribution(originator), **(extra_meta or {})},
    )
    engine._exec_sandbox_meta = sandbox_meta or {"sandbox": False, "network": False}
    engine._exec_profile = profile
    # Warm reflect only for real standalone runs: an injected client is a test
    # covered by the dream's cold-reflect catch-up, just later.
    if client is None:
        from rockycode.dream.reflect import attach_from_config
        attach_from_config(engine, workdir)
    approver = HeadlessApprover(engine, grants)
    engine.approver = approver
    return engine, approver


async def drive(
    engine: Engine,
    approver: HeadlessApprover,
    prompt: str,
    *,
    images: Optional[list[Path]] = None,
    write: Optional[Callable[[dict], None]] = None,
    include_thinking: bool = False,
    output_last_message: Optional[Path] = None,
    events: bool = True,
) -> int:
    """Run one exec turn: meta line, event stream, result envelope, exit code.
    `events=False` keeps stdout to meta → text → result (no tool.*/turn.*
    lines) — the receipt still lands in the trajectory."""
    from rockycode.session import public_id
    write = write or _stdout_line
    rk = public_id(engine.trajectory.session_id)
    profile = getattr(engine, "_exec_profile", "full")

    write({
        "type": "meta",
        "schema": SCHEMA,
        "session": rk,
        "model": engine.model,
        "resume": {"mode": "history-only", "requires_prompt": True,
                   "from_session": getattr(engine, "_exec_resumed_from", None)},
        "workdir": _scrub(str(engine.workdir)),
        # False warns the caller: overwrites here have no git safety net.
        "git": (engine.workdir / ".git").exists(),
        "profile": {
            "mode": PROFILE_MODE.get(profile, "workspace-write"),
            "name": profile,
            "tools": sorted(engine.registry),
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

    gen = engine.run_turn(prompt, images=images)
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
                # that's a budget stop (history can be carried forward), not a failure.
                if ev.message.startswith("step limit"):
                    budget_hit = True
                else:
                    error_msg = ev.message
            line = event_to_line(ev, include_thinking=include_thinking)
            if line and (events or line["type"] not in _EVENT_TYPES):
                write(line)
            if approver.blocked:
                break  # caller inspects effects, then requests an explicit follow-up
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
        "resumed_from": getattr(engine, "_exec_resumed_from", None),
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
    # Heuristic reward line, then the final reflect LAST (evolve v2): readers
    # take the last outcome record, so the graded reflect supersedes the
    # counters — this is what makes exec/routine runs gradeable. The envelope
    # above stays in the file as evidence.
    engine.finalize_outcome()
    await engine.run_final_hooks()

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
    workdir: Optional[Path] = None,
    resume: Optional[str] = None,
    images: Optional[list[Path]] = None,
    allowed_roots: tuple[Path, ...] = (),
    grants: frozenset[str] = frozenset(),
    max_steps: int = 30,
    originator: str = "",
    include_thinking: bool = False,
    output_last_message: Optional[Path] = None,
    write: Optional[Callable[[dict], None]] = None,
    client=None,
    model_choice=None,
    registry=None,
    sandbox: bool = True,
    network: bool = False,
    err=None,
    extra_meta: Optional[dict] = None,
    profile: str = "full",
    events: bool = False,
    max_tokens: Optional[int] = None,
    reasoning_effort: Optional[str] = None,
) -> int:
    """Provision the sandbox (full profile, default), build the engine, drive
    one task.

    `resume` forks chat-style history into a new session, then runs ONLY the
    explicit `prompt`. It never re-submits the original task or executes saved
    tool calls. The transcript is not a durable execution checkpoint: callers
    must verify effects before requesting retries, especially after a crash.
    Current launch flags supply permissions; old grants are never restored.

    The task can originate from an untrusted source (an issue, a page a
    delegating agent read), so by default every tool runs inside a Docker
    container with NO network: a `rm -rf /` hits the container's root, home
    secrets aren't mounted, and there's no egress to exfiltrate over. The
    command classifier stays on top as defense-in-depth, its real designed
    role. --no-sandbox is the explicit, loud host escape hatch.

    `events=False` (the default, and the CLI's) keeps stdout to meta → text →
    result; drive() itself defaults to the full receipt for library callers.

    A read/write profile has no shell at all — nothing executes, the file
    tools are jailed to the workdir — so it runs on the host with no Docker
    and starts instantly. That is the profile a calling agent wants for
    "look at this repo and tell me" or "write this small function".
    """
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {PROFILES}, got {profile!r}")
    if not prompt.strip():
        raise ValueError("an explicit non-empty task is required, including with --resume")
    if max_steps <= 0:
        raise ValueError("max_steps must be > 0 for headless exec")
    resume_info, history = None, []
    if resume is not None:
        from rockycode.session import load_resume_session
        resume_info, workdir, history = load_resume_session(resume, workdir=workdir)
    else:
        workdir = (workdir or Path.cwd()).expanduser().resolve()
        if not workdir.is_dir():
            raise ValueError(f"workdir '{workdir}' is not a directory")
    sb = None
    engine = None
    sandbox_meta = {"sandbox": False, "network": False}
    if profile != "full":
        if registry is None:
            registry = host_profile_registry(workdir, allowed_roots, profile)
        if err is not None:
            err.print(f"[dim]· profile {profile}: {PROFILE_MODE[profile]} · no shell, "
                      f"host file tools jailed to the workdir · no Docker needed[/]")
    elif sandbox and registry is None:  # registry injected → tests, already wired
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
        err.print(f"[bold {AMBER}]⚠ --no-sandbox: tools run on the HOST with no "
                  "isolation. The command classifier is a denylist, not a security "
                  "boundary — only use with input you fully trust.[/]")

    try:
        engine, approver = build_exec_engine(
            model=model, workdir=workdir, allowed_roots=allowed_roots, grants=grants,
            max_steps=max_steps, originator=originator, client=client, model_choice=model_choice, registry=registry,
            sandbox_meta=sandbox_meta, extra_meta=extra_meta, profile=profile,
            max_tokens=max_tokens, reasoning_effort=reasoning_effort,
        )
        if resume_info is not None:
            from rockycode.session import public_id
            engine.resume(history, from_session=resume_info.session_id)
            engine._exec_resumed_from = public_id(resume_info.session_id)
        if images:
            # A model the registry knows (deepseek-flash, kimi-k3, …) already
            # got the right vision flag from the Engine's launch resolution. For one it does NOT know — a custom endpoint served
            # via env — --image is the caller asserting the endpoint takes image
            # input, so trust the flag; a text-only endpoint rejects the request
            # with a clear API error in the JSONL stream.
            engine.vision_enabled = True
        return await drive(
            engine, approver, prompt, images=images,
            write=write, include_thinking=include_thinking,
            output_last_message=output_last_message, events=events,
        )
    finally:
        import asyncio
        import anyio
        # MCP cancellation is level-triggered under AnyIO. Shield teardown so a
        # disconnected caller cannot interrupt Docker/client cleanup immediately.
        with anyio.CancelScope(shield=True):
            if engine is not None:
                await engine.cancel_hooks()
            if sb is not None:
                try:
                    await asyncio.wait_for(sb.stop(), timeout=10)
                except Exception:  # noqa: BLE001 — best-effort teardown
                    pass
            if engine is not None and client is None:
                close = getattr(engine.client, "close", None)
                if close is not None:
                    try:
                        await asyncio.wait_for(close(), timeout=10)
                    except Exception:  # noqa: BLE001 — best-effort teardown
                        pass
