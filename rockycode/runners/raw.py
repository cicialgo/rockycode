"""Raw single-shot runner: model gets the problem statement and emits a unified
diff. No tools, no iteration. This is the lower-bound baseline against which
the rockycode harness's value-add is measured.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from openai import OpenAI
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from rockycode.banner import amaze, confused, fail
from rockycode.engine.effort import build_extra_body
from rockycode.onboarding import require_base_url, require_key
from rockycode.palette import PURPLE, VIOLET
from rockycode.prompts.rocky import RAW_SINGLE_SHOT
from rockycode.runners.data import load_verified

PREDICTIONS_DIR = Path("results") / "predictions"

# Pull the diff out of ```diff …``` first, then any fenced block, else raw.
_DIFF_FENCE = re.compile(r"```diff\s*\n(.*?)```", re.DOTALL)
_ANY_FENCE = re.compile(r"```\w*\s*\n(.*?)```", re.DOTALL)


def _extract_diff(text: str) -> str:
    if m := _DIFF_FENCE.search(text):
        return m.group(1)
    if m := _ANY_FENCE.search(text):
        return m.group(1)
    return text


def _build_prompt(instance: dict) -> str:
    return RAW_SINGLE_SHOT.format(
        repo=instance.get("repo", "unknown"),
        base_commit=instance.get("base_commit", "unknown"),
        problem_statement=instance.get("problem_statement", ""),
        hints=instance.get("hints_text", "") or "(none)",
    )


def _extract_usage(resp) -> dict:
    """Flatten resp.usage including DeepSeek-specific extras (cache hit/miss).

    DeepSeek surfaces `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens`
    in usage, which aren't in the openai-python SDK's typed Usage model.
    Pull them via model_dump so we don't lose them.
    """
    if resp.usage is None:
        return {}
    try:
        return resp.usage.model_dump()
    except AttributeError:
        return dict(resp.usage)


def run(
    model: str,
    instance_ids: Optional[list[str]],
    console: Console,
    *,
    thinking: bool = True,
    reasoning_effort: str = "max",
    max_tokens: int = 16384,
    task_label: str = "tasks",
    output_dir: Path = PREDICTIONS_DIR,
) -> Path:
    """Generate single-shot predictions for the requested instances.

    Returns the path to a SWE-bench-format predictions JSONL file.
    """
    ds = load_verified(console, instance_ids)
    console.print(f"  → {len(ds)} instances to predict\n")

    output_dir.mkdir(parents=True, exist_ok=True)
    safe_model = model.replace("/", "-")
    # task-set + timestamp: no run ever overwrites another
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    out_path = output_dir / f"raw-{safe_model}-{task_label}-{stamp}.jsonl"

    # Key AND endpoint from rocky's credential chain — never the SDK's ambient
    # OPENAI_API_KEY / OPENAI_BASE_URL fallbacks.
    # max_retries=5: DeepSeek is flakier than OpenAI; SDK default of 2 is too low.
    client = OpenAI(api_key=require_key(), base_url=require_base_url(),
                    max_retries=5, timeout=120.0)
    extra_body = build_extra_body(thinking, reasoning_effort)

    n_ok = n_fail = 0
    totals = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 0,
    }

    with out_path.open("w") as f, Progress(
        SpinnerColumn(style=PURPLE),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(complete_style=PURPLE, finished_style=VIOLET),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        bar = progress.add_task("predicting", total=len(ds))
        for instance in ds:
            iid = instance["instance_id"]
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": _build_prompt(instance)}],
                    temperature=0.0,
                    max_tokens=max_tokens,
                    extra_body=extra_body,
                )
                content = resp.choices[0].message.content or ""
                patch = _extract_diff(content).strip()
                if patch and not patch.endswith("\n"):
                    patch += "\n"

                usage = _extract_usage(resp)
                for k in totals:
                    totals[k] += usage.get(k, 0) or 0

                n_ok += 1
            except Exception as e:  # noqa: BLE001 — log + record empty patch, keep going
                fail(console, f"{iid}: {e}")
                patch = ""
                n_fail += 1

            f.write(json.dumps({
                "instance_id": iid,
                "model_name_or_path": f"raw-{safe_model}",
                "model_patch": patch,
            }) + "\n")
            f.flush()
            progress.update(bar, advance=1)

    if n_fail == 0:
        amaze(console, f"all {n_ok} predictions written to {out_path}!")
    else:
        confused(console, f"{n_ok} ok, {n_fail} failed. predictions at {out_path}")

    _print_usage_summary(console, totals)
    return out_path


def _print_usage_summary(console: Console, totals: dict) -> None:
    prompt = totals["prompt_tokens"]
    completion = totals["completion_tokens"]
    cache_hit = totals["prompt_cache_hit_tokens"]
    cache_miss = totals["prompt_cache_miss_tokens"]
    cache_seen = cache_hit + cache_miss
    hit_rate = (cache_hit / cache_seen * 100) if cache_seen else 0.0

    table = Table(title="token usage", show_header=True, header_style=f"bold {VIOLET}")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("prompt tokens", f"{prompt:,}")
    table.add_row("completion tokens", f"{completion:,}")
    table.add_row("cache hit tokens", f"{cache_hit:,}")
    table.add_row("cache miss tokens", f"{cache_miss:,}")
    table.add_row("cache hit rate", f"{hit_rate:.1f}%")
    console.print(table)
