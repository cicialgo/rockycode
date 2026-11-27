"""`rockycode dream` — the offline consolidation pass."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import typer

from rockycode.banner import confused, fail, info

# Credentials and endpoint come ONLY from ~/.rockycode (.env / keychain) or an
# explicit shell export. Project .env files are deliberately never loaded — a
# repo must not be able to supply a key or redirect where the key is sent
# (project_env_warnings tells the user when one tries).
from rockycode.cli._common import (  # noqa: F401
    console, REPO_ROOT, _config_model, _docker_preflight, _env_bool, _env_int, _load_prompt, _load_task_ids,
)


def dream(
    workdir: Optional[Path] = typer.Option(None, "--workdir", "-C", help="Project directory. Defaults to cwd."),
    model: Optional[str] = typer.Option(None, "--model", help="Model ID. Defaults to ROCKYCODE_MODEL env."),
    limit: int = typer.Option(10, "--limit", help="Max sessions to consolidate in one pass."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show decisions without writing anything "
                                                         "(still makes model calls)."),
) -> None:
    """Rocky sleeps: consolidate recent sessions into memory + proposals. ♪zzz"""
    import asyncio as _asyncio

    from rockycode.dream import DreamRunner

    wd = (workdir or Path.cwd()).resolve()
    model = model or os.getenv("ROCKYCODE_MODEL")
    if not model:
        fail(console, "no model. pass --model or set ROCKYCODE_MODEL in .env.")
        raise typer.Exit(1)
    console.print("[dim]♪zzz… rocky sleep. u watch something. i sort memory.[/dim]")

    # Evolve v2: every dream job runs on the session model, over the same
    # route the Engine would take for it (engine/route.py).
    try:
        from rockycode.engine import route as route_mod
        client = route_mod.open_client(route_mod.route(model), max_retries=3, timeout=120.0)
    except Exception as e:  # noqa: BLE001
        fail(console, f"dream runs on your session model and needs its key — {e}")
        raise typer.Exit(1)

    runner = DreamRunner(wd, client=client, model=model, dry_run=dry_run,
                         log=lambda s: console.print(f"  [dim]♪ {s}[/dim]"))

    index = None
    from rockycode.memory.index import IndexUnavailable, MemoryIndex
    try:
        index = MemoryIndex(runner.store)
        index.conn()
    except IndexUnavailable:
        index = None

    async def run_dream():
        try:
            return await runner.run(limit=limit, index=index)
        finally:
            await client.close()

    try:
        report = _asyncio.run(run_dream())
    except RuntimeError as e:
        confused(console, str(e))
        raise typer.Exit(1)
    except Exception as e:  # noqa: BLE001 — API trouble, bad key, network
        fail(console, f"dream failed ({type(e).__name__}: {e}).")
        raise typer.Exit(1)

    for line in report.decisions:
        console.print(f"  [dim]· {line}[/dim]")
    summary = (
        f"{report.sessions_digested} session(s) consolidated · "
        f"facts +{report.facts_added} ~{report.facts_updated} "
        f"archived {report.facts_archived} noop {report.facts_noop}"
    )
    if report.sessions_cold:
        summary += f" · cold-reflected {report.sessions_cold}"
    if report.patterns_added or report.patterns_reinforced:
        summary += f" · patterns +{report.patterns_added} ~{report.patterns_reinforced}"
    if report.proposals_drafted:
        summary += f" · drafted {report.proposals_drafted} proposal(s)"
        console.print("  [dim]· review drafts inside rockycode with /proposals[/dim]")
    if report.reindexed:
        summary += f" · re-embedded {report.reindexed[0]}"
    if dry_run:
        info(console, f"[dry-run] {summary}")
    elif report.sessions_digested == 0:
        info(console, "nothing new to dream about. rocky already remember everything.")
    else:
        console.print(f"[bold]✦ amaze! i wake. i remember better now.[/bold]  [dim]{summary}[/dim]")
