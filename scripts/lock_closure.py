#!/usr/bin/env python
"""Pin every sub-package a user install pulls in, to the versions in uv.lock.

pyproject.toml pins rocky's direct packages. Those pull in more, and a plain
`pip install rockycode` took whatever version of each was newest that day: a
poisoned new release of any of them would reach every new install.

This writes the whole tree into `[project].dependencies` and into each extra,
between marker lines, from `uv export --frozen`: the lockfile as it is, nothing
resolved, nothing fetched.

    python scripts/lock_closure.py          # rewrite the blocks in pyproject.toml
    python scripts/lock_closure.py --check  # exit 1 when a block is out of date
    python scripts/lock_closure.py --unpin  # drop the blocks, to upgrade a sub-package:
                                            # --unpin, uv lock --upgrade-package x==1.2, (no flag), uv lock

After a rewrite, run `uv lock` so the lockfile records the new metadata.
tests/smoke_locked_closure.py holds the same line in CI without uv.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
BEGIN = "    # ── sub-packages, pinned to uv.lock by scripts/lock_closure.py (do not edit by hand) ──\n"
END = "    # ── end of the pinned sub-packages ──\n"


def name_of(requirement: str) -> str:
    return re.sub(r"[-_.]+", "-", re.match(r"[A-Za-z0-9._-]+", requirement.strip()).group(0)).lower()


def exported(*extra: str) -> list[str]:
    """`name==version[ ; marker]` for one install, from the lockfile."""
    command = ["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes", "--no-header", "--no-annotate"]
    for name in extra:
        command += ["--extra", name]
    out = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True).stdout
    return [line.strip() for line in out.splitlines() if "==" in line and not line.startswith("#")]


def fill(text: str, opening: str, rows: list[str], known: set[str]) -> str:
    """Rewrite the generated block of the list that starts at `opening`."""
    start = text.index(f"\n{opening}") + 1
    stop = text.index("\n]", start) + 1
    body = text[start:stop]
    if BEGIN in body:
        body = body[:body.index(BEGIN)] + body[body.index(END) + len(END):]
    mine = {name_of(r) for r in re.findall(r'^\s*"([^"]+)"', body, re.M)} | known
    new = sorted((row for row in rows if name_of(row) not in mine), key=name_of)
    block = "".join(f'    "{row}",\n' for row in new)
    return text[:start] + body + (BEGIN + block + END if new else "") + text[stop:]


def rewritten(text: str) -> str:
    base = exported()
    text = fill(text, "dependencies = [", base, set())
    plain = {row for row in base}
    for extra in tomllib.loads(text)["project"]["optional-dependencies"]:
        # A package the default install already has under the same condition is pinned there.
        text = fill(text, f"{extra} = [", [row for row in exported(extra) if row not in plain], set())
    return text


def main() -> int:
    text = PYPROJECT.read_text(encoding="utf-8")
    if "--unpin" in sys.argv:
        PYPROJECT.write_text(re.sub(re.escape(BEGIN) + ".*?" + re.escape(END), "", text, flags=re.S), encoding="utf-8")
        return 0
    new = rewritten(text)
    if "--check" in sys.argv:
        if new != text:
            print("the pinned sub-packages are out of date: run scripts/lock_closure.py, then uv lock", file=sys.stderr)
            return 1
        print("pinned sub-packages match uv.lock")
        return 0
    PYPROJECT.write_text(new, encoding="utf-8")
    print("pyproject.toml rewritten. Now run: uv lock")
    return 0


if __name__ == "__main__":
    sys.exit(main())
