"""The rockycode harness runner: the agent loop on SWE-bench tasks.

Per task: pull the official SWE-bench image → start a container → Rocky
works inside it (bash/read/write/edit via docker exec) → `git diff` is the
prediction. Same images the scorer uses (namespace "swebench" on Docker
Hub), so nothing is built locally and the cache is shared.

Tasks run sequentially on purpose: containers run under emulation on
Apple Silicon, and one task at a time keeps API spend observable.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console

from rockycode.banner import amaze, confused, fail, info
from rockycode.engine.container import DockerSession, build_session_registry, extract_patch
from rockycode.palette import RED
from rockycode.engine.events import (
    Compacted,
    EngineError,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)
from rockycode.engine.loop import Engine
from rockycode.prompts.rocky import BENCH_TASK, ROCKY_SYSTEM
from rockycode.runners.data import load_verified

PREDICTIONS_DIR = Path("results") / "predictions"


def _image_for(instance: dict) -> str:
    from swebench.harness.test_spec.test_spec import make_test_spec

    return make_test_spec(instance, namespace="swebench").instance_image_key


async def _ensure_image(image: str, console: Console) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "docker", "image", "inspect", image,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    if await proc.wait() == 0:
        return True
    info(console, f"pulling {image} [dim](first time per task, can be ~1GB)[/dim]")
    proc = await asyncio.create_subprocess_exec(
        "docker", "pull", "--platform", "linux/amd64", image,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        fail(console, f"pull failed: {err.decode(errors='replace').strip().splitlines()[-1]}")
        return False
    return True


def _task_prompt(instance: dict) -> str:
    return BENCH_TASK.format(
        repo=instance.get("repo", "unknown"),
        problem_statement=instance.get("problem_statement", ""),
    )


async def _run_instance(
    instance: dict,
    *,
    model: str,
    thinking: bool,
    reasoning_effort: str,
    max_tokens: int,
    context_window: int,
    max_steps: int,
    system_prompt: str,
    prompt_name: str,
    prompt_sha: str,
    console: Console,
) -> dict:
    iid = instance["instance_id"]
    image = _image_for(instance)

    if not await _ensure_image(image, console):
        return {"instance_id": iid, "patch": "", "steps": 0, "usage": {}, "error": "image pull failed"}

    session = await DockerSession.start(image)
    try:
        registry = build_session_registry(session)
        # Same generated "# Tools this session" section as chat, built from
        # THIS registry — so bench prompts list exactly the container tools
        # (no phantom web/artifact advertisements burning budget steps). No
        # language/env/date appends: bench stays English + byte-reproducible.
        from rockycode.prompts.rocky import tools_section
        engine = Engine(
            model=model,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            context_window=context_window,
            max_steps=max_steps,
            system_prompt=system_prompt + tools_section(registry),
            registry=registry,
            trajectory_meta={
                "runner": "rockycode",
                "instance_id": iid,
                "image": image,
                "prompt_name": prompt_name,
                "prompt_sha": prompt_sha,
            },
        )

        steps, usage, err = 0, {}, None
        async for ev in engine.run_turn(_task_prompt(instance)):
            if isinstance(ev, ToolStarted):
                arg = (ev.args.get("raw") or "")[:70].replace("\n", " ")
                console.print(f"    [dim]⚒ {ev.tool} {arg}[/dim]")
            elif isinstance(ev, ToolFinished) and not ev.ok:
                first = ev.output.strip().splitlines()[0][:90] if ev.output.strip() else ""
                console.print(f"    [dim]✗ {ev.tool}: {first}[/dim]")
            elif isinstance(ev, Compacted):
                console.print(
                    f"    [dim]♻ compacted ({ev.strategy}): "
                    f"~{ev.tokens_before:,} → ~{ev.tokens_after:,} tokens[/dim]"
                )
            elif isinstance(ev, TextDelta):
                pass  # final summary text; trajectory has it
            elif isinstance(ev, EngineError):
                err = ev.message
                console.print(f"    [{RED}]✗ {ev.message}[/]")
            elif isinstance(ev, TurnFinished):
                steps, usage = ev.steps, ev.usage

        patch = await extract_patch(session)
        engine.trajectory.outcome(
            {
                "instance_id": iid,
                "steps": steps,
                "patch_chars": len(patch),
                "engine_error": err,
                "usage": usage,
            }
        )
        return {"instance_id": iid, "patch": patch, "steps": steps, "usage": usage, "error": err}
    finally:
        await session.stop()


async def _run_all(
    instances: list[dict],
    *,
    model: str,
    thinking: bool,
    reasoning_effort: str,
    max_tokens: int,
    context_window: int,
    max_steps: int,
    token_budget: int,
    system_prompt: str,
    prompt_name: str,
    prompt_sha: str,
    out_path: Path,
    safe_model: str,
    console: Console,
) -> None:
    totals: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "prompt_cache_hit_tokens": 0}
    n_patch = 0

    with out_path.open("w") as f:
        for i, instance in enumerate(instances, 1):
            spent = totals["prompt_tokens"] + totals["completion_tokens"]
            if token_budget and spent >= token_budget:
                info(console, f"token budget reached ({spent:,} >= {token_budget:,}) — stopping.")
                break

            iid = instance["instance_id"]
            console.print(f"\n[bold]task {i}/{len(instances)}[/bold] · {iid}")
            try:
                result = await _run_instance(
                    instance,
                    model=model,
                    thinking=thinking,
                    reasoning_effort=reasoning_effort,
                    max_tokens=max_tokens,
                    context_window=context_window,
                    max_steps=max_steps,
                    system_prompt=system_prompt,
                    prompt_name=prompt_name,
                    prompt_sha=prompt_sha,
                    console=console,
                )
            except Exception as e:  # noqa: BLE001 — one task must not kill the run
                fail(console, f"{iid}: {type(e).__name__}: {e}")
                result = {"instance_id": iid, "patch": "", "steps": 0, "usage": {}, "error": str(e)}

            patch = result["patch"]
            if patch.strip():
                n_patch += 1
                amaze(console, f"patch ready · {result['steps']} steps · {len(patch):,} chars")
            else:
                confused(console, f"no patch produced ({result.get('error') or 'agent stopped without changes'})")

            for k in totals:
                totals[k] += result["usage"].get(k, 0) or 0

            f.write(json.dumps({
                "instance_id": iid,
                "model_name_or_path": f"rockycode-{safe_model}",
                "model_patch": patch,
            }) + "\n")
            f.flush()

    console.print(
        f"\n[dim]· {n_patch}/{len(instances)} tasks produced a patch · "
        f"{totals['prompt_tokens']:,} in / {totals['completion_tokens']:,} out · "
        f"cache hit {totals['prompt_cache_hit_tokens']:,}[/dim]"
    )


def run(
    model: str,
    instance_ids: Optional[list[str]],
    console: Console,
    *,
    thinking: bool = True,
    reasoning_effort: str = "max",
    max_tokens: int = 16384,
    context_window: int = 131_072,
    max_steps: int = 50,
    token_budget: int = 0,
    system_prompt: Optional[str] = None,
    prompt_name: str = "rocky-builtin",
    prompt_sha: str = "",
    task_label: str = "tasks",
    output_dir: Path = PREDICTIONS_DIR,
) -> Path:
    """Run the harness on the requested instances. Returns predictions path."""
    if system_prompt is None:
        system_prompt = ROCKY_SYSTEM
    ds = load_verified(console, instance_ids)
    instances = list(ds)
    console.print(f"  → {len(instances)} tasks for the rockycode harness\n")

    output_dir.mkdir(parents=True, exist_ok=True)
    safe_model = model.replace("/", "-")
    # prompt + task-set + timestamp: no run ever overwrites another
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    out_path = output_dir / f"rockycode-{safe_model}-{prompt_name}-{task_label}-{stamp}.jsonl"

    asyncio.run(
        _run_all(
            instances,
            model=model,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            context_window=context_window,
            max_steps=max_steps,
            token_budget=token_budget,
            system_prompt=system_prompt,
            prompt_name=prompt_name,
            prompt_sha=prompt_sha,
            out_path=out_path,
            safe_model=safe_model,
            console=console,
        )
    )
    info(console, f"predictions at {out_path}; trajectories at .rockycode/trajectories/")
    return out_path
