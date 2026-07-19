"""Shared SWE-bench Verified dataset loading for all runners."""
from __future__ import annotations

from typing import Optional

from rich.console import Console

from rockycode.banner import confused, fail, info
from rockycode.palette import BLUE

_NETWORK_HINTS = (
    "connection", "timeout", "name resolution", "network", "unreachable",
    "dns", "ssl", "max retries", "getaddrinfo", "proxy", "refused",
)

DATASET = "princeton-nlp/SWE-bench_Verified"


def load_verified(console: Console, instance_ids: Optional[list[str]]):
    """Load Verified (filtered to instance_ids if given). Exits with a
    friendly message on network failure."""
    import datasets

    # HF's tqdm bars (download + filter) collide with our Rich status line —
    # one renderer must own the terminal.
    datasets.disable_progress_bars()

    try:
        with console.status(
            f"loading [{BLUE}]{DATASET}[/] "
            "[dim](first run pulls ~hundreds of MB from huggingface)[/dim]",
            spinner="dots",
        ):
            ds = datasets.load_dataset(DATASET, split="test")
    except Exception as e:
        fail(console, "could not load SWE-bench Verified from huggingface.")
        msg = str(e).lower()
        if any(k in msg for k in _NETWORK_HINTS):
            confused(
                console,
                "looks like a network issue. check internet / vpn / proxy / "
                "is huggingface.co reachable?",
            )
        else:
            confused(console, f"underlying error: {type(e).__name__}: {e}")
        info(console, "isolate the load step to debug:")
        info(
            console,
            "  uv run python -c \"from datasets import load_dataset; "
            "load_dataset('princeton-nlp/SWE-bench_Verified', split='test')\"",
        )
        raise SystemExit(1)

    if instance_ids:
        wanted = set(instance_ids)
        ds = ds.filter(lambda r: r["instance_id"] in wanted)
        missing = wanted - set(ds["instance_id"])
        if missing:
            confused(console, f"{len(missing)} requested IDs not in Verified: {sorted(missing)[:3]}…")

    return ds
