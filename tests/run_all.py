#!/usr/bin/env python
"""Run the smoke-test suite — the pre-commit / CI gate.

CORE tests are pure: fake DeepSeek streams, no network, no API key, no docker.
They run on every commit and in CI, and a single failure exits non-zero.

A test that genuinely needs a container runtime goes in the DOCKER set below
(skipped by default, listed explicitly — nothing silently omitted; pass --all
to include it locally).

    python tests/run_all.py          # CORE gate (what CI runs)
    python tests/run_all.py --all     # + docker-dependent sandbox lifecycle tests
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Tests that genuinely need a container runtime (docker exec). smoke_container.py
# still drives LocalSession — plain local bash — so it remains in the CORE gate.
DOCKER: set[str] = {"smoke_docker_sandbox_cancel.py"}

# Isolate the global store (~/.rockycode) into a throwaway temp home so the
# suite never writes trajectories into the real one.
_ENV = {**os.environ, "ROCKYCODE_HOME": tempfile.mkdtemp(prefix="rockytest-home-")}


def run(path: Path) -> tuple[bool, str]:
    r = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, env=_ENV)
    out = (r.stdout + r.stderr).strip()
    last = out.splitlines()[-1] if out else ""
    return r.returncode == 0, last


def main() -> int:
    include_docker = "--all" in sys.argv
    tests = sorted(HERE.glob("smoke_*.py"))
    core = [t for t in tests if t.name not in DOCKER]
    extra = [t for t in tests if t.name in DOCKER]
    to_run = core + (extra if include_docker else [])

    failed: list[str] = []
    for t in to_run:
        ok, last = run(t)
        print(f"  {'PASS' if ok else 'FAIL'}  {t.name}" + ("" if ok else f"   → {last[:110]}"))
        if not ok:
            failed.append(t.name)

    if extra and not include_docker:
        print(f"  SKIP  (need docker; run with --all): {', '.join(t.name for t in extra)}")

    n = len(to_run)
    if failed:
        print(f"\n{n - len(failed)}/{n} passed · {len(failed)} FAILED: {', '.join(failed)}")
        return 1
    print(f"\n{n}/{n} passed — amaze!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
