"""`rockycode serve` — the JSON-RPC host for editor clients."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from rockycode.banner import fail

# Credentials and endpoint come ONLY from ~/.rockycode (.env / keychain) or an
# explicit shell export. Project .env files are deliberately never loaded — a
# repo must not be able to supply a key or redirect where the key is sent
# (project_env_warnings tells the user when one tries).
from rockycode.cli._common import (  # noqa: F401
    console, REPO_ROOT, _config_model, _docker_preflight, _env_bool, _env_int, _load_prompt, _load_task_ids,
)


def serve(
    model: Optional[str] = typer.Option(None, help="Model ID. Defaults to ROCKYCODE_MODEL env."),
    workdir: Optional[Path] = typer.Option(
        None, "--workdir", "-C", help="Project directory (default: cwd)."
    ),
    thinking: bool = typer.Option(
        _env_bool("ROCKYCODE_THINKING", True),
        "--thinking/--no-thinking",
    ),
    reasoning_effort: str = typer.Option(
        os.getenv("ROCKYCODE_REASONING_EFFORT", "max"),
        "--reasoning-effort",
    ),
    max_tokens: int = typer.Option(
        _env_int("ROCKYCODE_MAX_TOKENS", 0), "--max-tokens",
        help="0 = the model's registry cap.",
    ),
    context_window: int = typer.Option(
        _env_int("ROCKYCODE_CONTEXT_WINDOW", 0), "--context-window",
        help="0 = the model's registry window.",
    ),
    max_steps: int = typer.Option(
        _env_int("ROCKYCODE_CHAT_MAX_STEPS", 0), "--max-steps",
    ),
    prompt: Optional[Path] = typer.Option(
        None, "--prompt",
        help="System prompt file (default: built-in ROCKY_SYSTEM).",
    ),
) -> None:
    """Start Rocky as a JSON-RPC 2.0 server over stdin/stdout (for editor clients)."""
    import asyncio

    from rockycode.server import run_server

    model = model or os.getenv("ROCKYCODE_MODEL") or _config_model()
    if not model:
        # stdout is the JSON-RPC channel — a Rich error there corrupts the very
        # first bytes the client reads. Startup errors go to stderr.
        fail(Console(stderr=True, highlight=False), "no model. pass --model, set ROCKYCODE_MODEL in .env, or "
             "`rockycode config model <spec>`.")
        raise typer.Exit(1)

    workdir = (workdir or Path.cwd()).resolve()

    # Load custom system prompt if provided
    system_prompt = os.getenv("ROCKYCODE_SYSTEM_PROMPT", "")
    if prompt:
        system_prompt = prompt.read_text()

    asyncio.run(run_server(
        model=model, workdir=workdir,
        thinking=thinking, reasoning_effort=reasoning_effort,
        max_tokens=max_tokens or None, context_window=context_window or None,
        max_steps=max_steps, system_prompt=system_prompt,
    ))
