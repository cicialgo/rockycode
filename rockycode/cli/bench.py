"""`rockycode bench …` — SWE-bench runs and scoring."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer

from rockycode.banner import confused, fail, info, show_banner

# Credentials and endpoint come ONLY from ~/.rockycode (.env / keychain) or an
# explicit shell export. Project .env files are deliberately never loaded — a
# repo must not be able to supply a key or redirect where the key is sent
# (project_env_warnings tells the user when one tries).
from rockycode.cli._common import (  # noqa: F401
    console, REPO_ROOT, _config_model, _docker_preflight, _env_bool, _env_int, _load_prompt, _load_task_ids,
    _require_route,
)


bench_app = typer.Typer()


@bench_app.callback(invoke_without_command=True)
def bench(
    ctx: typer.Context,
    runner: str = typer.Option("raw", help="'raw' (single-shot) or 'rockycode' (harness, v1+)."),
    tasks: str = typer.Option("dev10", help="'dev10', 'verified', or path to a JSON list of instance IDs."),
    model: Optional[str] = typer.Option(None, help="Model ID. Defaults to ROCKYCODE_MODEL env."),
    limit: Optional[int] = typer.Option(None, help="Cap number of tasks."),
    run_id: Optional[str] = typer.Option(None, help="Run label. Defaults to runner-model-timestamp."),
    skip_score: bool = typer.Option(False, "--skip-score", help="Generate predictions only."),
    prompt: Optional[Path] = typer.Option(
        None, "--prompt",
        help="System prompt file for the rockycode runner (default: built-in ROCKY_SYSTEM).",
    ),
    thinking: bool = typer.Option(
        _env_bool("ROCKYCODE_THINKING", True),
        "--thinking/--no-thinking",
        help="Enable the model's thinking mode (env: ROCKYCODE_THINKING).",
    ),
    reasoning_effort: str = typer.Option(
        os.getenv("ROCKYCODE_REASONING_EFFORT", "max"),
        "--reasoning-effort",
        help="Reasoning depth when thinking is on: low | high | max, clamped onto "
             "the provider's own tiers at the wire (env: ROCKYCODE_REASONING_EFFORT).",
    ),
    max_tokens: int = typer.Option(
        _env_int("ROCKYCODE_MAX_TOKENS", 0),
        "--max-tokens",
        help="Per-call output cap, including thinking. 0 = model registry; use 16384 for the old budget.",
    ),
    context_window: int = typer.Option(
        _env_int("ROCKYCODE_CONTEXT_WINDOW", 0),
        "--context-window",
        help="Context window in tokens. 0 = model registry, as in chat; pin explicitly for reproducible runs.",
    ),
    max_steps: int = typer.Option(
        _env_int("ROCKYCODE_MAX_STEPS", 100),
        "--max-steps",
        help="Main calls per task, then 10 closing calls; neutral resource notices (env: ROCKYCODE_MAX_STEPS).",
    ),
    token_budget: int = typer.Option(
        _env_int("ROCKYCODE_TOKEN_BUDGET", 0),
        "--token-budget",
        help="Max total prompt+completion tokens across all tasks. 0 = unlimited (env: ROCKYCODE_TOKEN_BUDGET).",
    ),
) -> None:
    """Run rockycode against a SWE-bench task set and report the score."""
    if ctx.invoked_subcommand:
        return  # a subcommand (`bench score`) runs instead of a bench run
    show_banner(console)

    model = model or os.getenv("ROCKYCODE_MODEL") or _config_model()
    if not model:
        fail(console, "no model. pass --model, set ROCKYCODE_MODEL in .env, or "
             "`rockycode config model <spec>`.")
        raise typer.Exit(1)
    _require_route(model)  # before Docker and the dataset: a missing key is one line, not a traceback

    if reasoning_effort not in {"low", "high", "max", "xhigh"}:
        fail(console, f"invalid --reasoning-effort '{reasoning_effort}'. use low, high, or max.")
        raise typer.Exit(1)

    # raw needs docker only for scoring; the rockycode harness always needs it
    # (the agent works inside the task containers).
    if runner == "rockycode" or not skip_score:
        _docker_preflight()

    instance_ids = _load_task_ids(tasks)
    if limit and instance_ids:
        instance_ids = instance_ids[:limit]

    count = len(instance_ids) if instance_ids else "all (500)"
    # task-set label goes into the predictions filename so dev10/test20
    # runs never overwrite each other
    task_label = tasks if tasks in ("dev10", "verified") else Path(tasks).stem
    info(console, f"runner={runner}  tasks={tasks}  count={count}")
    info(console, f"model={model}  thinking={thinking}  effort={reasoning_effort}  max_tokens={max_tokens}")
    if token_budget:
        info(console, f"token budget={token_budget:,} (stops when total prompt+completion tokens exceed this)")

    if runner == "raw":
        if prompt is not None:
            confused(console, "--prompt only affects the rockycode runner; raw uses its fixed template.")
        from rockycode.runners.raw import run as run_raw
        predictions_path = run_raw(
            model=model,
            instance_ids=instance_ids,
            console=console,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            task_label=task_label,
        )
    elif runner == "rockycode":
        system_prompt, prompt_name, prompt_sha = _load_prompt(prompt)
        info(console, f"prompt={prompt_name} [dim]sha {prompt_sha}[/dim]")
        from rockycode.runners.agent import run as run_agent
        predictions_path = run_agent(
            model=model,
            instance_ids=instance_ids,
            console=console,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            context_window=context_window,
            max_steps=max_steps,
            token_budget=token_budget,
            system_prompt=system_prompt,
            prompt_name=prompt_name,
            prompt_sha=prompt_sha,
            task_label=task_label,
        )
    else:
        fail(console, f"unknown runner: {runner}")
        raise typer.Exit(1)

    if skip_score:
        info(console, f"predictions saved to {predictions_path}. score skipped.")
        return

    if not run_id:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_id = f"{runner}-{model.replace('/', '-')}-{ts}"

    from rockycode.score import score
    score(predictions_path=predictions_path, instance_ids=None, run_id=run_id, console=console)


@bench_app.command("score")
def bench_score(
    predictions: Path = typer.Argument(
        ..., help="Predictions JSONL from a bench run (results/predictions/…)."
    ),
    run_id: Optional[str] = typer.Option(
        None,
        help="Eval run id. Defaults to the predictions filename, so re-running "
             "the SAME command resumes a crashed eval (already-scored instances "
             "are skipped by the swebench harness).",
    ),
    max_workers: int = typer.Option(4, help="Parallel eval containers."),
    timeout: int = typer.Option(1800, help="Per-instance eval timeout in seconds."),
) -> None:
    """Score an existing predictions file — or resume a crashed scoring run.

    The agent phase and the scoring phase are separable: if scoring dies
    (network, docker), the predictions file still holds the full run. This
    re-runs ONLY the eval, with a run_id stable across retries by default.
    """
    show_banner(console)
    if not predictions.exists():
        fail(console, f"predictions file not found: {predictions}")
        raise typer.Exit(1)
    _docker_preflight()
    from rockycode.score import score
    score(
        predictions_path=predictions,
        run_id=run_id or predictions.stem,
        instance_ids=None,
        console=console,
        max_workers=max_workers,
        timeout=timeout,
    )
