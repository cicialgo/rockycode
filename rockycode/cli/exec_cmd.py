"""`rockycode exec` — rocky as a subagent for other agents (JSONL contract)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console

from rockycode.banner import fail

# Credentials and endpoint come ONLY from ~/.rockycode (.env / keychain) or an
# explicit shell export. Project .env files are deliberately never loaded — a
# repo must not be able to supply a key or redirect where the key is sent
# (project_env_warnings tells the user when one tries).
from rockycode.cli._common import (  # noqa: F401
    console, REPO_ROOT, _config_model, _docker_preflight, _env_bool, _env_int, _load_prompt, _load_task_ids,
    _require_route,
)


# exec's local error exit — mirrors headless.EXIT_ERROR without importing the
# engine at module import time (the CLI must stay fast for --help).
EXIT_CODE_ERROR = 1

def exec_cmd(
    prompt: Optional[str] = typer.Argument(
        None,
        help="The task. Omit or pass '-' to read it from stdin (for long prompts "
             "piped by a calling agent).",
    ),
    resume: Optional[str] = typer.Option(
        None, "--resume", "-r",
        help="Carry a session's history into a NEW session. Requires an explicit "
             "follow-up task (or '-' for stdin); no picker or automatic tool replay.",
    ),
    allow: Optional[List[str]] = typer.Option(
        None, "--allow",
        help="Grant a result.blocked_on.grant token for this invocation only "
             "(repeatable, e.g. --allow pkg-install --allow delete). Does not "
             "override the profile or unconditional command refusals.",
    ),
    workdir: Optional[Path] = typer.Option(
        None, "--workdir", "-C",
        help="Project directory. Defaults to cwd, or the session's current folder "
             "with --resume. An explicit conflicting directory is rejected.",
    ),
    allow_dir: Optional[List[Path]] = typer.Option(
        None, "--allow-dir",
        help="Extra directory write/edit may touch beyond the workdir (repeatable).",
    ),
    model: Optional[str] = typer.Option(None, help="Model ID. Defaults to ROCKYCODE_MODEL env."),
    image: Optional[List[Path]] = typer.Option(
        None, "--image",
        help="Image file to attach to the task (repeatable). Needs a vision-capable "
             "model (deepseek-flash sees; so do kimi-k3, minimax-m3, glm-5.3-flash, "
             "qwen3.8-*, mimo-v2.6-pro, step-5-preview, openrouter-free); sent as a base64 data URL.",
    ),
    profile: str = typer.Option(
        "full", "--profile",
        help="What rocky may do: read (read_file/grep/glob/view_image only — no "
             "shell, no writes, no Docker needed) · write (+ write_file/edit_file "
             "jailed to --workdir/--allow-dir, still no shell, no Docker) · full "
             "(+ bash, sandboxed in Docker by default). A calling agent that only "
             "wants an answer or a small edit should pick read/write: sub-second "
             "start, nothing to isolate.",
    ),
    events: bool = typer.Option(
        False, "--events/--no-events",
        help="Stream every tool.started/tool.finished/turn.* event line. Off by "
             "default: the caller's context is the scarce resource, so stdout is "
             "meta → text → result only (the trajectory keeps the full receipt).",
    ),
    max_steps: int = typer.Option(
        30, "--max-steps",
        help="Tool-step budget (must be > 0 — headless runs are never unbounded). "
             "Exhaustion exits 3; use --resume with an explicit follow-up and "
             "a larger budget to carry the saved history forward.",
    ),
    output_last_message: Optional[Path] = typer.Option(
        None, "--output-last-message", "-o",
        help="Also write the final answer text to this file.",
    ),
    include_thinking: bool = typer.Option(
        False, "--include-thinking",
        help="Emit the model's reasoning as `thinking` events (off by default — "
             "they bloat the calling agent's context). Implies --events.",
    ),
    originator: str = typer.Option(
        "", "--originator",
        help="Calling agent self-identification (e.g. 'claude-code'), recorded in "
             "the trajectory's audit trail. env: ROCKYCODE_ORIGINATOR.",
    ),
    sandbox: bool = typer.Option(
        True, "--sandbox/--no-sandbox",
        help="Run every tool inside a Docker container (default ON). The task can "
             "come from an untrusted source, so isolation — not the command "
             "classifier — is the real boundary. --no-sandbox runs on the host "
             "(UNSAFE for untrusted input; needs no Docker).",
    ),
    network: bool = typer.Option(
        False, "--network/--no-network",
        help="Give the sandbox network access (default OFF — no egress, so a "
             "delegated task can't exfiltrate or phone home). Turn on only when "
             "the task genuinely needs to fetch something.",
    ),
) -> None:
    """Headless one-shot for OTHER coding agents: run one task, stream JSONL, exit.

    stdout is JSONL only. First line: `meta` {schema: rockyexec/1, session:
    rk_…, profile}. Then `text` / `tool.started` / `tool.finished` / `error`
    events. Last line: `result` {status, summary, blocked_on, evidence:
    {files_changed, commands, refused}, usage} — evidence, not verdicts:
    the caller verifies. Everything human goes to stderr.

    Exit codes: 0 done · 1 error · 2 blocked on an action needing a grant
    (result.blocked_on.grant says which) · 3 step budget spent.

    Permissions follow --profile: read (no writes, no shell), write (edits
    stay inside --workdir + --allow-dir roots, no shell), full (bash in a
    Docker sandbox). Destructive/irreversible commands are always refused —
    no flag disables that. Deletes, pushes, installs, and sudo stop the run
    at exit 2. Continue with --resume <id> --allow <grant> and an explicit
    follow-up task. This carries history into a NEW session, like chat resume;
    it does NOT restore an execution checkpoint or replay pending tool calls.
    Inspect prior effects before requesting a retry: interrupted calls have
    unknown outcomes. There is no exactly-once guarantee. Grants, profile,
    sandbox, network and extra roots come from THIS invocation, not history.
    """
    err = Console(stderr=True, highlight=False)  # stdout belongs to the JSONL contract
    if profile not in ("read", "write", "full"):
        fail(err, f"--profile must be read | write | full (got {profile!r}).")
        raise typer.Exit(EXIT_CODE_ERROR)
    model = model or os.getenv("ROCKYCODE_MODEL") or _config_model()
    if not model:
        fail(err, "no model. pass --model, set ROCKYCODE_MODEL in .env, or "
             "`rockycode config model <spec>`.")
        raise typer.Exit(EXIT_CODE_ERROR)
    # The model's own route (engine/route.py): a task on another provider or on a
    # local server needs no home key, and a missing key is one friendly line.
    _require_route(model, err)
    if max_steps <= 0:
        fail(err, "--max-steps must be > 0: headless runs are never unbounded.")
        raise typer.Exit(EXIT_CODE_ERROR)

    if resume is not None and prompt is None:
        fail(err, "--resume requires an explicit follow-up task (or '-' for stdin); "
                  "automatic grant-and-continue is not supported.")
        raise typer.Exit(EXIT_CODE_ERROR)
    if prompt is None or prompt == "-":
        if sys.stdin.isatty():
            fail(err, "no task. pass it as an argument or pipe it on stdin.")
            raise typer.Exit(EXIT_CODE_ERROR)
        prompt = sys.stdin.read()
    if not prompt.strip():
        fail(err, "empty task.")
        raise typer.Exit(EXIT_CODE_ERROR)

    wd = workdir.expanduser().resolve() if workdir is not None else None
    if wd is not None and not wd.is_dir():
        fail(err, f"--workdir '{wd}' is not a directory.")
        raise typer.Exit(EXIT_CODE_ERROR)
    allowed_roots: tuple[Path, ...] = ()
    if allow_dir:
        roots = []
        for d in allow_dir:
            rp = d.expanduser().resolve()
            if not rp.is_dir():
                fail(err, f"--allow-dir '{d}' is not a directory.")
                raise typer.Exit(EXIT_CODE_ERROR)
            roots.append(rp)
        allowed_roots = tuple(roots)

    images: list[Path] = []
    if image:
        from rockycode.engine.images import IMAGE_EXTS
        for ip in image:
            p = ip.expanduser().resolve()
            if not p.is_file():
                fail(err, f"--image '{ip}' is not a file.")
                raise typer.Exit(EXIT_CODE_ERROR)
            if p.suffix.lower() not in IMAGE_EXTS:
                fail(err, f"--image '{ip}': unsupported format. "
                          f"use {' / '.join(sorted(IMAGE_EXTS))}.")
                raise typer.Exit(EXIT_CODE_ERROR)
            images.append(p)

    import asyncio

    from rockycode.headless import run_exec

    try:
        code = asyncio.run(run_exec(
            prompt=prompt, model=model, workdir=wd, images=images or None,
            resume=resume, grants=frozenset(allow or ()), allowed_roots=allowed_roots,
            max_steps=max_steps, originator=originator,
            include_thinking=include_thinking, output_last_message=output_last_message,
            sandbox=sandbox, network=network, err=err,
            profile=profile, events=events or include_thinking,
        ))
    except ValueError as e:
        fail(err, str(e))
        raise typer.Exit(EXIT_CODE_ERROR)
    raise typer.Exit(code)
