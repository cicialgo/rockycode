"""`rockycode goal` — an autonomous, budget-capped run from the shell."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from rockycode.banner import fail, info
from rockycode.palette import AMBER, VIOLET

# Credentials and endpoint come ONLY from ~/.rockycode (.env / keychain) or an
# explicit shell export. Project .env files are deliberately never loaded — a
# repo must not be able to supply a key or redirect where the key is sent
# (project_env_warnings tells the user when one tries).
from rockycode.onboarding import project_env_warnings  # noqa: E402
from rockycode.cli._common import (  # noqa: F401
    console, REPO_ROOT, _config_model, _docker_preflight, _env_bool, _env_int, _launch_model, _load_prompt,
    _load_task_ids,
)


def goal(
    objective: Optional[str] = typer.Argument(None, help="What to accomplish autonomously (omit with --clean)."),
    model: Optional[str] = typer.Option(None, help="Model for the work turns (default: ROCKYCODE_MODEL)."),
    reviewer_model: Optional[str] = typer.Option(
        None, "--reviewer-model",
        help="Model for milestone review (default: same as --model). Set deepseek-v4-pro "
             "for stronger review — it costs more.",
    ),
    max_usd: Optional[float] = typer.Option(None, "--max-usd", help="Spend ceiling in your currency (default: recommended)."),
    max_hours: Optional[float] = typer.Option(None, "--max-hours", help="Wallclock limit in hours (default: recommended 8h)."),
    max_tokens: Optional[int] = typer.Option(None, "--max-tokens", help="Total-token cap."),
    review_every: int = typer.Option(3, "--review-every", help="Milestone-review cadence, in turns."),
    yes: bool = typer.Option(False, "--yes", help="Auto-approve ask-tier commands (git push, sudo, installs) and network."),
    network: Optional[bool] = typer.Option(
        None, "--network/--no-network",
        help="Give the sandbox internet access. Default: OFF (offline) — safer "
             "unattended. If the objective looks like it needs network (pip/apt/"
             "download), rocky asks you up front. Pass --network to force it on.",
    ),
    workdir: Path = typer.Option(Path.cwd(), "--workdir", help="Project root."),
    clean: bool = typer.Option(False, "--clean", help="Prune leftover goal worktrees (keeps branches), then exit."),
    context_file: Optional[Path] = typer.Option(None, "--context-file", hidden=True),
    result_file: Optional[Path] = typer.Option(None, "--result-file", hidden=True),
) -> None:
    """Run rocky AUTONOMOUSLY toward an objective — on an isolated copy of the repo,
    in the sandbox, under a hard budget. Review the result as a git branch."""
    import asyncio
    import time as _time

    from rockycode.config import load as load_config
    from rockycode.engine import route as route_mod
    from rockycode.goal.budget import GoalBudget, recommended
    from rockycode.goal.runner import EngineDriver, GoalRunner, safe_bash_tool
    from rockycode.engine.loop import Engine
    from rockycode.engine.sandbox import ChatSandbox, build_sandbox_registry
    from rockycode.goal.worktree import GoalWorkspace
    from rockycode.pricing import UsageLedger
    from rockycode.session import HOME_ROOT

    if clean:
        from rockycode.goal.worktree import prune_goal_worktrees
        removed = prune_goal_worktrees(workdir)
        if removed:
            info(console, f"pruned {len(removed)} goal worktree(s) — branches kept:")
            for p in removed:
                console.print(f"  [dim]{p}[/]")
        else:
            info(console, "no leftover goal worktrees to prune.")
        # Goal logs are small step-summaries (not raw output), but pile up one per
        # run — drop ones older than 14 days, keep recent ones for review.
        log_dir = HOME_ROOT / "goal-logs"
        old = 0
        if log_dir.exists():
            for lg in log_dir.glob("*.log"):
                try:
                    if _time.time() - lg.stat().st_mtime > 14 * 86400:
                        lg.unlink()
                        old += 1
                except OSError:
                    pass
        if old:
            info(console, f"removed {old} goal log(s) older than 14 days.")
        info(console, f"goal logs: {log_dir}  [dim](small; review with `cat`)[/]")
        raise typer.Exit(0)
    if not objective:
        fail(console, "give an objective to run, or --clean to prune old goal worktrees.")
        raise typer.Exit(1)
    goal_context = ""  # chat digest on a /goal handoff — seeds planning
    if context_file and context_file.exists():
        try:
            goal_context = context_file.read_text()[:8000]
        except OSError:
            pass

    model = _launch_model(model)  # first run: paste-your-key, then continue
    reviewer_model = reviewer_model or model
    # Each model's own route, checked before any workspace exists: the work
    # model and the reviewer may live on different providers.
    try:
        work, review = route_mod.route(model), route_mod.route(reviewer_model)
    except route_mod.RouteError as e:
        fail(console, str(e))
        raise typer.Exit(1)
    currency = load_config(workdir)["currency"]
    if max_usd is None and max_hours is None and max_tokens is None:
        budget = recommended(currency)
    else:
        budget = GoalBudget(
            max_usd=max_usd,
            max_seconds=max_hours * 3600 if max_hours is not None else None,
            max_tokens=max_tokens,
            currency=currency,
        )

    async def _run() -> None:
        from rockycode.engine.safety import network_intent, pre_scan

        slug = _time.strftime("%Y%m%d-%H%M%S")
        ws = GoalWorkspace.create(workdir.resolve(), slug)
        info(console, f"isolated workspace: {ws.path}" + (f"  ·  branch {ws.branch}" if ws.branch else "  (copy)"))
        # Persistent per-run log (plan + every step + verify reasons) so the run is
        # reviewable after it scrolls away — the pointer is printed at the end and
        # surfaced when a chat hands off and resumes.
        log_dir = HOME_ROOT / "goal-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{slug}.log"

        def _log(msg: str) -> None:
            try:
                with log_path.open("a", encoding="utf-8") as f:
                    f.write(msg + "\n")
            except OSError:
                pass
        _log(f"# goal: {objective}\n# {slug}  workspace={ws.path}  branch={ws.branch}")
        ledger = UsageLedger()
        for w in project_env_warnings(Path.cwd()):
            info(console, w)
        client = route_mod.open_client(work)
        reviewer_client = client if reviewer_model == model else route_mod.open_client(review)
        # Plan BEFORE the sandbox exists (planning is LLM-only), so network/permits
        # are decided from the REAL plan, not a guess at your wording. Engine is
        # attached after the pre-flight decision.
        from rockycode.engine.explore import make_goal_verifier
        driver = EngineDriver(client=client, model=model, reviewer_model=reviewer_model,
                              reviewer_client=reviewer_client,
                              workspace=ws, ledger=ledger, currency=currency,
                              verifier=make_goal_verifier(
                                  client=client, model=model, workdir=ws.path, ledger=ledger,
                                  engine_cls=lambda **kwargs: Engine(**kwargs, route=work)))

        # Plan → derive permits → confirm, in a REFINE loop (all before the sandbox
        # exists, so it's cheap to iterate). At the gate: 'y' proceed, 'e' edit
        # (give guidance, re-plan, re-confirm), 'n' cancel. Proceeding grants the
        # plan's permits; --network/--no-network force the net setting; --yes auto.
        # Initial plan against the real files (folding in any chat-handoff context).
        plan_input = objective if not goal_context else (
            f"{objective}\n\n[Context from the chat that led here — use it to inform "
            f"the plan; the objective above is the goal]:\n{goal_context}")
        info(console, f"planning: {objective}")
        try:
            plan, requires = await driver.plan(plan_input)
        except Exception as e:  # noqa: BLE001
            fail(console, f"planning failed — {e}")
            ws.cleanup(keep=False)
            raise typer.Exit(1)
        if not plan:
            fail(console, "the planner produced no milestones.")
            ws.cleanup(keep=False)
            raise typer.Exit(1)

        # Confirm loop: show plan → derive permits → gate. 'e' opens a real
        # back-and-forth — rocky ANSWERS your question, then shows the (revised or
        # same) plan — all before the sandbox exists, so iterating is cheap.
        while True:
            for i, m in enumerate(plan, 1):
                console.print(f"  [dim]{i}.[/] {m}")

            # derive needs FROM THE PLAN — the planner's REQUIRES line + a scan backstop
            scan_text = "\n".join(plan) + (("\n" + requires) if requires else "")
            flags = pre_scan(scan_text)
            blocked = [v for v in flags if v.action == "block"]
            if blocked:
                fail(console, f"plan names a blocked action: {blocked[0].reason}")
                ws.cleanup(keep=False)
                raise typer.Exit(1)
            asks = [v for v in flags if v.action == "ask"]
            net_reason = network_intent(requires) or network_intent(scan_text)
            use_network = network if network is not None else bool(net_reason)
            approved: set[str] = {v.pattern for v in asks}
            if use_network or asks or net_reason:
                info(console, "this run will need — approve before you leave:")
                if use_network:
                    console.print(f"  [{VIOLET}]🌐 network[/] — {net_reason or 'requested'}")
                elif net_reason:
                    console.print(f"  [dim]· plan implies network ({net_reason}) but --no-network is set — those steps will fail[/]")
                for v in asks:
                    console.print(f"  [{AMBER}]⬆ {v.reason}[/]")
            else:
                info(console, "this run needs no extra permissions (offline, no privileged commands).")

            if yes:
                break
            # Explicit verbs so 'n' can't be misread as "no, let's talk" — that's 'e'.
            GO, EDIT, CANCEL = ("y", "yes", ""), ("e", "edit", "discuss"), ("n", "no", "c", "cancel")
            valid = GO + EDIT + CANCEL
            action = "?"
            while action not in valid:
                action = typer.prompt(
                    "  run this plan?  [y] yes (enter)  ·  [e] discuss/edit  ·  [n] cancel",
                    default="y", show_default=False).strip().lower()
                if action not in valid:
                    console.print("  [dim](y = run it · e = discuss/edit the plan · n = cancel)[/]")
            if action in GO:
                break
            if action in CANCEL:
                info(console, "cancelled — nothing ran. [dim](tip: 'e' discusses/edits the plan instead of cancelling)[/]")
                ws.cleanup(keep=False)
                raise typer.Exit(0)
            # edit → discuss: rocky answers, then re-shows the (possibly revised) plan
            msg = typer.prompt("  ask about or change the plan").strip()
            if msg:
                try:
                    reply, plan, requires = await driver.discuss(objective, plan, requires, msg)
                except Exception as e:  # noqa: BLE001
                    reply = f"(couldn't reason about that — {type(e).__name__})"
                if reply:
                    console.print(f"\n  [#8b6fc9]rocky:[/] [italic]{reply}[/]\n")
            # loop → re-show the (possibly revised) plan + re-gate
        info(console, f"sandbox network: {'ON' if use_network else 'off (offline)'}")
        _log("plan:\n" + "\n".join(f"  {i}. {m}" for i, m in enumerate(plan, 1)))

        # 4) NOW provision the sandbox with that decision and attach the engine
        try:
            sandbox = await ChatSandbox.start(ws.path, network=use_network)
        except Exception as e:  # noqa: BLE001
            fail(console, f"goal mode needs the sandbox (Docker) — {e}")
            ws.cleanup(keep=False)
            raise typer.Exit(1)
        reg = build_sandbox_registry(sandbox)
        reg["bash"] = safe_bash_tool(sandbox, approved)  # approvals frozen up front
        engine = Engine(model=model, client=client, route=work, workdir=ws.path, registry=reg,
                        trajectory_meta={"goal": objective, "runner": "goal"})
        driver.attach(engine, network=use_network)

        # 5) run the loop with the pre-computed plan (tee events to console + log)
        def _on_event(m: str) -> None:
            console.print(f"[dim]· {m}[/]")
            _log(m)
        runner = GoalRunner(objective, driver, budget, ws, ledger, review_every=review_every,
                            on_event=_on_event, preplanned=plan)
        info(console, f"budget — {budget.preflight_note()}")
        try:
            result = await runner.run()
        finally:
            await sandbox.stop()

        sym = "¥" if currency == "cny" else "$"
        info(console, f"goal {result.status}: {result.reason}")
        info(console, f"{result.milestones_done}/{result.milestones_total} milestones · "
                      f"spend {sym}{ledger.cost(currency):.4f}")
        if ws.branch:
            # Work is committed on the goal branch; the worktree dir is kept so
            # you can run/poke the files. Branch is never auto-deleted.
            info(console, f"review:  git -C {ws.origin} diff {ws.base or 'HEAD'}..{ws.branch}   (merge if good)")
            console.print(f"  run it:  cd {ws.path}")
            console.print(f"  tidy up: git -C {ws.origin} worktree remove {ws.path}   (keeps branch {ws.branch})")
        else:
            info(console, f"review the work in: {ws.path}")
        _log(f"result: {result.status} — {result.reason}  "
             f"({result.milestones_done}/{result.milestones_total} milestones)")
        info(console, f"log:   {log_path}   [dim](full plan + per-step verify — check it anytime)[/]")
        if result_file is not None:  # a /goal handoff reads this to resume chat
            import json as _json
            try:
                result_file.write_text(_json.dumps({
                    "status": result.status, "reason": result.reason,
                    "branch": ws.branch, "origin": str(ws.origin),
                    "milestones_done": result.milestones_done,
                    "milestones_total": result.milestones_total, "log": str(log_path),
                }))
            except OSError:
                pass

    asyncio.run(_run())
