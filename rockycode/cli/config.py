"""`rockycode config` and `rockycode pricing`."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import typer

from rockycode.banner import fail, info
from rockycode.palette import LAVENDER

# Credentials and endpoint come ONLY from ~/.rockycode (.env / keychain) or an
# explicit shell export. Project .env files are deliberately never loaded — a
# repo must not be able to supply a key or redirect where the key is sent
# (project_env_warnings tells the user when one tries).
from rockycode.cli._common import (  # noqa: F401
    console, REPO_ROOT, _config_model, _docker_preflight, _env_bool, _env_int, _load_prompt, _load_task_ids,
)


def config(
    key: Optional[str] = typer.Argument(None, help="Config key to read or set."),
    value: Optional[str] = typer.Argument(None, help="New value (omit to read)."),
) -> None:
    """Show or set rockycode preferences (currency, theme, language, ui_language)."""
    from rockycode.config import DEFAULTS, GLOBAL_PATH, change_feedback, load, set_value
    if key is None:
        resolved = load(Path.cwd())
        info(console, f"config file: {GLOBAL_PATH}")
        for k in DEFAULTS:
            console.print(f"  [{LAVENDER}]{k}[/] = {resolved[k]}")
        return
    if value is None:
        console.print(f"{key} = {load(Path.cwd()).get(key)}")
        return
    v, err = set_value(key, value)
    if err:
        fail(console, err)
        raise typer.Exit(1)
    info(console, f"saved: {key} = {v}  →  {GLOBAL_PATH}")
    feedback = change_feedback(key, Path.cwd())
    if feedback:
        info(console, feedback)


def pricing() -> None:
    """Show the token price table (USD + CNY) and peak-hour status."""
    from datetime import timezone

    from rockycode.pricing import (
        OVERRIDE_PATH, PRICING_SOURCE_URL, PRICING_VERIFIED, _is_peak, load_pricing,
    )
    from rockycode.engine.providers import MODELS_TOML
    p = load_pricing()
    info(console, f"verified {PRICING_VERIFIED} · source {PRICING_SOURCE_URL}")
    info(console, f"prices are registry data — add/correct a model's price in {MODELS_TOML}")
    info(console, f"or override the assembled table in {OVERRIDE_PATH} (no reinstall)")
    console.print()
    for model, rates in p["models"].items():
        sched = f" · peak schedule: {rates['peak']}" if rates.get("peak") else ""
        console.print(f"  [{LAVENDER}]{model}[/] [dim](per 1M tokens{sched})[/dim]")
        for cur in ("usd", "cny"):
            r = rates.get(cur)
            if not r:
                console.print(f"    {cur.upper()}: [dim]not set[/dim]")
                continue
            sym = "¥" if cur == "cny" else "$"
            console.print(
                f"    {cur.upper()}: in-hit {sym}{r['in_hit']} · "
                f"in-miss {sym}{r['in_miss']} · out {sym}{r['out']}"
            )
    peak = p.get("peak", {})
    console.print()
    if peak.get("enabled"):
        wins = ", ".join(f"{w['start']}–{w['end']}" for w in peak.get("windows_utc", []))
        active = "ACTIVE now" if _is_peak(datetime.now(timezone.utc), peak) else "not active right now"
        days = " · Mon–Fri, CN holidays off-peak" if peak.get("weekdays_only") else ""
        console.print(
            f"  [{LAVENDER}]peak surcharge (deepseek)[/] ×{peak.get('multiplier')} · UTC {wins}{days} · "
            f"from {peak.get('effective_date', '?')} · [dim]{active}[/dim]"
        )
    else:
        console.print(f"  [{LAVENDER}]peak surcharge[/] [dim]disabled[/dim]")
