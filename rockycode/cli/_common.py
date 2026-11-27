"""Shared by the command modules: the console, the repo root, small env/config helpers."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from rockycode.banner import fail, info

# Credentials and endpoint come ONLY from ~/.rockycode (.env / keychain) or an
# explicit shell export. Project .env files are deliberately never loaded — a
# repo must not be able to supply a key or redirect where the key is sent
# (project_env_warnings tells the user when one tries).
# highlight=False: Rich's auto-highlighter paints numbers, paths, URLs and quoted
# text in ANSI cyan / magenta / blue / green. rocky's colours come from the palette.
console = Console(highlight=False)

# rockycode/cli/_common.py → the checkout root (bench task files live under it).
REPO_ROOT = Path(__file__).resolve().parents[2]


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on", "y"}


def _config_model() -> Optional[str]:
    """Config key `model` — the sticky default when neither --model nor
    ROCKYCODE_MODEL is set. GLOBAL config only, by design: a cloned repo's
    project config must never pick which endpoint requests go to. Scriptable:
    `rockycode config model <spec>` (a /model-style spec; the engine resolves
    it against the provider registry at launch)."""
    from rockycode.config import load as _load_cfg
    return str(_load_cfg().get("model") or "").strip() or None


def _require_route(model: str, out: Optional[Console] = None) -> None:
    """A command with nobody to ask (exec, bench) stops with one line when its
    model has no usable route — a missing key, a local server that is down."""
    from rockycode.engine import route as route_mod
    try:
        route_mod.route(model)
    except route_mod.RouteError as error:
        fail(out or console, str(error))
        raise typer.Exit(1)


def _launch_model(model: Optional[str]) -> str:
    """The model an interactive command launches on, with a route that works
    (engine/route.py). Nothing set up yet → the first-run flow. A named model
    that only lacks its key → the flow asks for that one key. Anything else
    that cannot run (a local server that is down, a model that does not exist)
    ends here with what to do, and so does every case without a terminal to ask in."""
    import sys

    from rockycode.cli.firstrun import run_setup
    from rockycode.engine import route as route_mod

    def named() -> Optional[str]:
        return model or os.getenv("ROCKYCODE_MODEL") or _config_model()

    def stop(message: str):
        fail(console, message)
        raise typer.Exit(1)

    spec, missing = named(), None
    if spec:
        try:
            route_mod.route(spec)
            return spec
        except route_mod.RouteError as error:
            if not error.key_env:
                stop(str(error))
            missing = error
    if not sys.stdin.isatty():
        stop(str(missing) if missing else
             "nothing is set up yet — run `rockycode` in a terminal once, or set ROCKYCODE_MODEL and its key.")
    run_setup(console, missing)
    spec = named()
    if not spec:
        stop("no model. pass --model, set ROCKYCODE_MODEL in .env, or `rockycode config model <spec>`.")
    try:
        route_mod.route(spec)
    except route_mod.RouteError as error:
        stop(str(error))
    return spec


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _load_task_ids(tasks: str) -> Optional[list[str]]:
    """Resolve --tasks into an instance_id list, or None for the full Verified set."""
    if tasks == "verified":
        return None
    if tasks == "dev10":
        path = REPO_ROOT / "bench" / "tasks" / "dev10.json"
    else:
        path = Path(tasks)
    if not path.exists():
        raise typer.BadParameter(f"task file not found: {path}")
    return json.loads(path.read_text())


def _load_prompt(prompt_path: Optional[Path]) -> tuple[str, str, str]:
    """Resolve --prompt into (system_prompt, name, sha8).

    Default is the built-in ROCKY_SYSTEM; a file swaps it wholesale. The
    name+sha land in trajectory meta so A/B runs stay distinguishable.
    """
    import hashlib

    if prompt_path is None:
        from rockycode.prompts.rocky import ROCKY_SYSTEM
        text, name = ROCKY_SYSTEM, "rocky-builtin"
    else:
        if not prompt_path.exists():
            raise typer.BadParameter(f"prompt file not found: {prompt_path}")
        text, name = prompt_path.read_text(), prompt_path.stem
    sha = hashlib.sha256(text.encode()).hexdigest()[:8]
    return text, name, sha


def _docker_preflight() -> None:
    """Bail with a friendly message if the Docker daemon isn't reachable.

    Runs before any API call so the user doesn't burn tokens on a run
    that was always going to fail at the scoring step.
    """
    try:
        proc = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        fail(console, "`docker` command not found.")
        info(console, "install Docker Desktop (https://www.docker.com/products/docker-desktop)")
        info(console, "or pass --skip-score to generate predictions without scoring.")
        raise typer.Exit(1)
    except subprocess.TimeoutExpired:
        fail(console, "docker daemon check timed out (10s).")
        info(console, "is Docker Desktop running? open it and wait for the whale icon to be steady.")
        raise typer.Exit(1)

    if proc.returncode != 0:
        fail(console, "docker daemon not reachable. scoring needs docker.")
        info(console, "open Docker Desktop and wait for the whale 🐳 in your menu bar to be steady.")
        info(console, "or pass --skip-score to generate predictions only.")
        first_err = (proc.stderr or proc.stdout or "").strip().splitlines()
        if first_err:
            info(console, f"detail: {first_err[0]}")
        raise typer.Exit(1)
