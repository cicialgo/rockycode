"""stdio MCP server so Codex can delegate a task to `rockycode exec`.

This is not `rockycode serve`. serve speaks rocky's own JSON-RPC for editor
clients. This process speaks Model Context Protocol on stdin/stdout: logs and
tracebacks go to stderr, because a single non-protocol byte on stdout breaks
the client.

The backwards-compatible task tool is `rocky_exec`. It calls `engine.headless.run_exec` in-process
(the same path as the `rockycode exec` command) and returns that run's result
envelope. The profile defaults to `read` and is never widened here — write
and full happen only when the caller passes them.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from rockycode.mcp_features import jobs, lifespan, register, root_path, task_context, close_jobs

from rockycode.headless import (
    EXIT_ERROR,
    _scrub,
    _scrub_obj,
)

DEFAULT_MODEL = "deepseek-flash"
DEFAULT_PROFILE = "read"
DEFAULT_MAX_STEPS = 30  # same cap as `rockycode exec --max-steps`
PROFILES = ("read", "write", "full")

# Codex uses the first 512 characters when it decides how to use the server,
# so the profile rule and the data-leaves warning both have to fit in there.
INSTRUCTIONS = (
    "Rocky capabilities: call rocky_capabilities first. Default exec profile is read. "
    "Write/full, grants, memory writes, proposal approval and Goal execution require "
    "user authorization through the calling host. Selected task/files/context go to "
    "the configured model provider. Never send secrets. For long work use rocky_submit "
    "then rocky_job_status/cancel. Nonzero exit_code is not success. Resume carries "
    "history, never automatically replays tools. Decisions are advice, not permission."
)


class RockyExecResult(BaseModel):
    """Structured payload of one rocky_exec call.

    A non-zero exit is still a normal MCP result: the caller needs the
    envelope (blocked_on, evidence), not a protocol error.
    """

    status: str
    exit_code: int
    summary: str = ""
    session: Optional[str] = None
    blocked_on: Optional[dict[str, Any]] = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    steps: int = 0
    usage: dict[str, Any] = Field(default_factory=dict)
    cost_usd: Optional[float] = None
    peak: Optional[bool] = None
    output_path: Optional[str] = None
    error: Optional[str] = None
    summary_truncated: bool = False
    resumed_from: Optional[str] = None
    profile: str
    model: str


def _log(message: str) -> None:
    print(_scrub(message), file=sys.stderr, flush=True)


def _fail(message: str, *, profile: str, model: str, code: int = EXIT_ERROR) -> RockyExecResult:
    return RockyExecResult(
        status="error",
        exit_code=code,
        summary="",
        evidence={"files_changed": [], "commands": [], "refused": []},
        error=_scrub(message),
        profile=profile,
        model=model,
    )


def _price(model: str, usage: dict[str, Any]) -> tuple[Optional[float], Optional[bool]]:
    """USD cost of this run's usage, or (None, None) when nothing was priced.

    An unknown model is not reported as $0 — that would look free. Peak-hour
    doubling is applied by the same ledger the rest of rocky uses.
    """
    if not usage:
        return None, None
    try:
        from rockycode.pricing import UsageLedger

        ledger = UsageLedger()
        ledger.add(model, usage)
        if not ledger.priced(model):
            return None, None
        peak = any(flag for (name, flag) in ledger.buckets if name == model)
        return ledger.cost("usd"), peak
    except Exception:  # noqa: BLE001 — a pricing glitch must not drop the answer
        return None, None


def _write_output(summary: str) -> Optional[str]:
    """Drop the scrubbed answer in a 0600 temp file.

    The read profile must not write into the project. Codex can still open
    this path when it truncates the inline tool result. The path is not
    scrubbed: rewriting `~` into it would make the file unopenable.
    """
    if not summary:
        return None
    path: Optional[str] = None
    try:
        fd, path = tempfile.mkstemp(prefix="rocky-mcp-", suffix=".txt")
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(summary)
            if not summary.endswith("\n"):
                fh.write("\n")
    except OSError as e:
        _log(f"rockycode mcp: could not write output file: {e}")
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
        return None
    return path


def _from_envelope(
    envelope: dict[str, Any],
    *,
    code: int,
    profile: str,
    model: str,
) -> RockyExecResult:
    summary = _scrub(str(envelope.get("summary") or ""))
    usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
    usage = _scrub_obj(usage)
    evidence = envelope.get("evidence") if isinstance(envelope.get("evidence"), dict) else {}
    cost, peak = _price(model, usage)
    error = envelope.get("error")
    blocked = envelope.get("blocked_on")
    return RockyExecResult(
        status=str(envelope.get("status") or "error"),
        exit_code=int(code),
        summary=summary[:24_000],
        summary_truncated=len(summary) > 24_000,
        resumed_from=envelope.get("resumed_from"),
        session=envelope.get("session") if isinstance(envelope.get("session"), str) else None,
        blocked_on=_scrub_obj(blocked) if isinstance(blocked, dict) else None,
        evidence=_scrub_obj(evidence) if isinstance(evidence, dict) else {},
        steps=int(envelope.get("steps") or 0),
        usage=usage if isinstance(usage, dict) else {},
        cost_usd=cost,
        peak=peak,
        output_path=_write_output(summary),
        error=_scrub(str(error)) if error else None,
        profile=profile,
        model=model,
    )


async def run_delegated(
    task: str,
    *,
    model: str = DEFAULT_MODEL,
    profile: str = DEFAULT_PROFILE,
    workdir: Optional[str] = None,
    max_steps: Optional[int] = None,
    resume: Optional[str] = None,
    grants: Optional[list[str]] = None,
    skills: Optional[list[str]] = None,
    memory_names: Optional[list[str]] = None,
    include_user_skills: bool = False,
    max_tokens: Optional[int] = None,
    effort: Optional[str] = None,
    timeout_s: int = 1800,
) -> RockyExecResult:
    """Run one headless exec and shape its envelope for an MCP client."""
    model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    # Empty / missing profile stays read. Never substitute write or full.
    profile = (profile or DEFAULT_PROFILE).strip().lower() or DEFAULT_PROFILE
    steps = DEFAULT_MAX_STEPS if max_steps is None else max_steps

    if profile not in PROFILES:
        return _fail(
            f"profile must be read, write, or full (got {profile!r}).",
            profile=profile, model=model,
        )
    if not isinstance(steps, int) or isinstance(steps, bool) or not 1 <= steps <= 1000:
        return _fail("max_steps must be an integer in 1..1000.", profile=profile, model=model)
    if not task or not str(task).strip():
        return _fail("task is empty.", profile=profile, model=model)

    if not 1 <= timeout_s <= 28_800:
        return _fail("timeout_s must be 1..28800.", profile=profile, model=model)
    if max_tokens is not None and (isinstance(max_tokens, bool) or not 1024 <= max_tokens <= 524_288):
        return _fail("max_tokens must be 1024..524288 (Engine minimum is 1024).", profile=profile, model=model)
    if effort not in (None, "off", "low", "high", "max"):
        return _fail("effort must be off, low, high, or max.", profile=profile, model=model)
    if grants and not resume:
        return _fail("grants require a resumed session and explicit user authorization.",
                     profile=profile, model=model)
    if any(not isinstance(g, str) or not g or len(g) > 200 for g in (grants or [])):
        return _fail("invalid grant token.", profile=profile, model=model)
    try:
        if resume:
            from rockycode.session import load_resume_session
            _, root, _ = load_resume_session(resume, workdir=Path(workdir) if workdir else None)
        else:
            root = root_path(workdir)
        task = task_context(task, root, skills, memory_names, include_user_skills)
        # Credentials are selected by Engine per provider, not pre-checked as DeepSeek.
        from rockycode.onboarding import bootstrap_credentials
        bootstrap_credentials()
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc), profile=profile, model=model)

    from rich.console import Console

    from rockycode.headless import run_exec

    lines: list[dict] = []
    def capture(line):
        # Long text/event streams need not accumulate beside the final envelope.
        if isinstance(line, dict) and line.get("type") == "result":
            lines[:] = [line]

    _log(f"rockycode mcp: rocky_exec profile={profile} model={model} steps={steps}")
    try:
        async with asyncio.timeout(timeout_s):
            code = await run_exec(
                prompt=task,
                model=model,
                workdir=root,
                profile=profile,
                max_steps=steps,
                resume=resume,
                grants=frozenset(grants or []),
                max_tokens=max_tokens,
                reasoning_effort=effort,
                originator=os.getenv("ROCKYCODE_ORIGINATOR", "") or "mcp",
                extra_meta={"runner": "mcp"},
                # full keeps exec's defaults: Docker sandbox, no network. read/write
                # never start Docker. There is no flag here to turn the sandbox off.
                sandbox=True,
                network=False,
                events=False,
                write=capture,
                err=Console(stderr=True, highlight=False),
            )
    except Exception as e:  # noqa: BLE001 — stderr only; the client gets a scrubbed envelope
        _log(traceback.format_exc())
        return _fail(f"{type(e).__name__}: {e}", profile=profile, model=model)

    envelope = next(
        (line for line in reversed(lines) if isinstance(line, dict) and line.get("type") == "result"),
        None,
    )
    if envelope is None:
        # Docker-missing and other pre-drive failures return an exit code and
        # write nothing. That is still an error envelope for the caller.
        result = _fail(
            "exec produced no result envelope.",
            profile=profile, model=model,
            code=code if isinstance(code, int) and code != 0 else EXIT_ERROR,
        )
        _log(f"rockycode mcp: rocky_exec status=error exit={result.exit_code}")
        return result

    result = _from_envelope(envelope, code=int(code), profile=profile, model=model)
    _log(f"rockycode mcp: rocky_exec status={result.status} exit={result.exit_code}")
    return result


mcp = FastMCP(
    "rocky",
    instructions=INSTRUCTIONS,
    log_level="WARNING",
    lifespan=lifespan,
    stateless_http=False,
)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Run rockycode exec",
        readOnlyHint=False,
        destructiveHint=True,
        openWorldHint=True,
    ),
)
async def rocky_exec(
    task: str,
    model: str = DEFAULT_MODEL,
    profile: str = DEFAULT_PROFILE,
    workdir: Optional[str] = None,
    max_steps: Optional[int] = None,
    resume: Optional[str] = None,
    grants: Optional[list[str]] = None,
    skills: Optional[list[str]] = None,
    memory_names: Optional[list[str]] = None,
    include_user_skills: bool = False,
    max_tokens: Optional[int] = None,
    effort: Optional[str] = None,
    timeout_s: int = 1800,
) -> RockyExecResult:
    """Delegate a task (read by default; write/full require user authorization).

    resume forks a transcript with this explicit follow-up; verify prior effects first.
    grants are blocked_on.grant tokens authorized by the user for this invocation only.
    skills/memory_names select context sent to the provider; no ambient bulk injection.
    max_tokens is per model call, not a total budget. effort=off/low/high/max.
    full uses Docker without network. No host-shell or sandbox-disable switch.
    """
    async with jobs.lane:
        return await run_delegated(
            task, model=model, profile=profile, workdir=workdir, max_steps=max_steps,
            resume=resume, grants=grants, skills=skills, memory_names=memory_names,
            include_user_skills=include_user_skills, max_tokens=max_tokens,
            effort=effort, timeout_s=timeout_s,
        )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def rocky_submit(
    task: str, model: str = DEFAULT_MODEL, profile: str = DEFAULT_PROFILE,
    workdir: Optional[str] = None, max_steps: Optional[int] = None,
    resume: Optional[str] = None, grants: Optional[list[str]] = None,
    skills: Optional[list[str]] = None, memory_names: Optional[list[str]] = None,
    include_user_skills: bool = False, max_tokens: Optional[int] = None,
    effort: Optional[str] = None, timeout_s: int = 1800,
) -> dict[str, Any]:
    """Start a background exec with the same contract as rocky_exec. Poll job.id with rocky_job_status.

    Jobs survive this tool's return, not a server restart. Cancel explicitly before retrying;
    retries create new work and may duplicate effects. No automatic re-submission.
    """
    if not 1 <= timeout_s <= 28_800:
        raise ValueError("timeout_s must be 1..28800")
    async def run(emit):
        result = await run_delegated(
            task, model=model, profile=profile, workdir=workdir, max_steps=max_steps,
            resume=resume, grants=grants, skills=skills, memory_names=memory_names,
            include_user_skills=include_user_skills, max_tokens=max_tokens,
            effort=effort, timeout_s=timeout_s,
        )
        return result.model_dump()
    return jobs.submit("exec", run, timeout_s=timeout_s)


register(mcp)


def serve_stdio() -> None:
    """Block, serving MCP on stdin/stdout. Logs stay on stderr."""
    _log("rockycode mcp: stdio server ready (capability bridge, default profile read)")
    mcp.run(transport="stdio")


class BearerAuth:
    """Protect every HTTP request; no unauthenticated localhost or browser access."""
    def __init__(self, app, token):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        import hmac
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            supplied = headers.get(b"authorization", b"")
            if not hmac.compare_digest(supplied, ("Bearer " + self.token).encode()):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await self.app(scope, receive, send)


def http_app(token: str):
    from contextlib import asynccontextmanager
    mcp.settings.stateless_http = True
    app = mcp.streamable_http_app()
    original = app.router.lifespan_context

    @asynccontextmanager
    async def outer_lifespan(application):
        try:
            async with original(application):
                yield
        finally:
            await close_jobs()
    app.router.lifespan_context = outer_lifespan
    return BearerAuth(app, token)


def serve_http(port: int = 8765) -> None:
    """Single-user loopback HTTP bridge for URL-based local MCP clients."""
    import uvicorn
    token = os.getenv("ROCKYCODE_MCP_TOKEN", "")
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("set ROCKYCODE_MCP_TOKEN to a random ASCII token of at least 32 characters")
    if not 1 <= port <= 65535:
        raise ValueError("port must be 1..65535")
    mcp.settings.port = port
    # FastMCP's loopback Host/Origin checks remain enabled (DNS rebinding defense).
    app = http_app(token)
    _log(f"rockycode mcp: authenticated loopback HTTP on 127.0.0.1:{port}/mcp")
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False, log_level="warning")
