"""Wraps SWE-bench's official evaluation harness as a subprocess.

We call `python -m swebench.harness.run_evaluation` rather than importing
internals — the harness's public CLI is its stable contract; its Python API
moves between releases.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table

from rockycode.banner import amaze, fail, info
from rockycode.palette import VIOLET

DATASET = "princeton-nlp/SWE-bench_Verified"


def score(
    predictions_path: Path,
    run_id: str,
    instance_ids: Optional[list[str]],
    console: Console,
    max_workers: int = 4,
    timeout: int = 1800,
) -> dict:
    """Run swebench eval and parse the report. Returns the parsed report dict."""
    info(console, f"scoring run_id={run_id}")
    info(console, f"predictions={predictions_path}")

    cmd = [
        sys.executable, "-m", "swebench.harness.run_evaluation",
        "--dataset_name", DATASET,
        "--predictions_path", str(predictions_path),
        "--max_workers", str(max_workers),
        "--run_id", run_id,
        "--timeout", str(timeout),
    ]
    if instance_ids:
        cmd += ["--instance_ids", *instance_ids]

    info(console, f"$ {' '.join(cmd)}")
    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0:
        fail(console, f"swebench harness exited {proc.returncode}")
        raise SystemExit(proc.returncode)

    report = _find_report(run_id)
    if report is None:
        fail(console, "no report file found after eval. check logs/run_evaluation/")
        raise SystemExit(1)

    _print_report(console, report, run_id)
    return report


def _find_report(run_id: str) -> Optional[dict]:
    """The harness writes its summary as <model_name>.<run_id>.json in cwd
    (older versions) or under logs/run_evaluation/<run_id>/ (newer). Match run_id
    EXACTLY (delimited) and take the NEWEST match: a loose `*run_id*` glob matched
    'v1' inside a 'v12' report, and with no freshness order a re-run of the same
    run_id could pick up a stale report."""
    candidates: list[Path] = []
    candidates += list(Path(".").glob(f"*.{run_id}.json"))
    candidates += list(Path("logs/run_evaluation").glob(f"{run_id}/**/results.json"))
    candidates += list(Path("logs/run_evaluation").glob(f"{run_id}/**/*.json"))
    candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and ("resolved_ids" in data or "resolved_instances" in data):
                return data
        except (json.JSONDecodeError, OSError):
            continue
    return None


def _count(report: dict, ids_key: str, count_key: str) -> int:
    v = report.get(ids_key)
    if isinstance(v, list):
        return len(v)
    c = report.get(count_key)
    return int(c) if isinstance(c, (int, float)) else 0


def _print_report(console: Console, report: dict, run_id: str) -> None:
    resolved = _count(report, "resolved_ids", "resolved_instances")
    submitted = _count(report, "submitted_ids", "submitted_instances")
    error = _count(report, "error_ids", "error_instances")
    total = report.get("total_instances") or 0
    # Score over what we actually submitted. swebench's total_instances is the
    # whole Verified set (500) — dividing by it made a 1-task run read as 0.2%.
    pct = (resolved / submitted * 100) if submitted else 0.0

    info(console, f"run: {run_id}")
    table = Table(show_header=True, header_style=f"bold {VIOLET}")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("submitted", str(submitted))
    table.add_row("resolved", str(resolved))
    table.add_row("errors", str(error))
    table.add_row("score (resolved / submitted)", f"[bold]{pct:.1f}%[/bold]")
    table.add_row("[dim]verified set size[/dim]", f"[dim]{total}[/dim]")
    console.print(table)

    if pct > 0:
        amaze(console, f"score {pct:.1f}%! amaze!")
    else:
        console.print("[dim]we no resolve any. we try harness next, learn more.[/dim]")
