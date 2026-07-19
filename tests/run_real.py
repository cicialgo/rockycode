#!/usr/bin/env python
"""Run the REAL (network, token-spending) tests. Local / pre-release only.

Each real_*.py skips itself (exit 0) when creds are absent, so this is safe to
run anywhere — it just won't test much without ROCKYCODE_API_KEY + ROCKYCODE_MODEL.
Deliberately kept OUT of CI: the repo is MIT/public and the API key stays local.

    python tests/run_real.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    reals = sorted(HERE.glob("real_*.py"))
    if not reals:
        print("(no real_*.py tests yet)")
        return 0
    failed: list[str] = []
    for t in reals:
        print(f"── {t.name} " + "─" * (60 - len(t.name)))
        if subprocess.run([sys.executable, str(t)]).returncode != 0:
            failed.append(t.name)
    if failed:
        print(f"\nFAILED: {', '.join(failed)}")
        return 1
    print("\nreal tests OK (or skipped for missing creds)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
