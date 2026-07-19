"""Smoke test for the post-edit check-runner (engine/checks.py). No network.

Verifies subprocess plumbing (exit codes + captured output + missing-binary +
timeout) and repo detection, plus a real end-to-end run when ruff is installed.
"""
import asyncio
import shutil
import tempfile
from pathlib import Path

from rockycode.engine import checks


async def main():
    wd = Path(tempfile.mkdtemp(prefix="rockychecks-"))

    # _run_one: exit code + output captured
    code, out = await checks._run_one(["python3", "-c", "print('hello')"], wd, 30)
    assert code == 0 and "hello" in out, (code, out)
    code, out = await checks._run_one(
        ["python3", "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"], wd, 30
    )
    assert code == 3 and "boom" in out, (code, out)

    # missing binary → 127, never raises
    code, _ = await checks._run_one(["definitely-not-a-real-cmd-xyz"], wd, 30)
    assert code == 127, code

    # timeout → 124, process killed
    code, out = await checks._run_one(["python3", "-c", "import time; time.sleep(10)"], wd, 1)
    assert code == 124 and "timed out" in out, (code, out)
    print("checks._run_one: exit codes, missing-binary, timeout  ✓")

    # detect_checks: ruff when installed; bundled pyflakes fallback otherwise
    (wd / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    real_which = shutil.which
    checks.shutil.which = lambda c: f"/usr/bin/{c}" if c == "ruff" else None
    try:
        det_ruff = checks.detect_checks(wd)
    finally:
        checks.shutil.which = real_which
    # argv[0] is now the resolved path, so match on label + the tail
    assert any(lbl == "ruff" and argv[-2:] == ["check", "."] for lbl, argv in det_ruff), det_ruff

    checks.shutil.which = lambda c: None  # nothing installed
    try:
        det_fallback = checks.detect_checks(wd)
    finally:
        checks.shutil.which = real_which
    assert det_fallback and det_fallback[0][0] == "pyflakes", det_fallback
    print("checks.detect_checks: ruff when present, bundled pyflakes fallback otherwise  ✓")

    # check_code end-to-end via the bundled pyflakes fallback: catch a real bug
    (wd / "bug.py").write_text("def f():\n    return undefined_name\n")
    checks.shutil.which = lambda c: None  # force the fallback path
    try:
        tool = checks.build_check_tool(wd)["check_code"]
        report = await tool.fn()
    finally:
        checks.shutil.which = real_which
    assert "pyflakes" in report and "FAILED" in report and "undefined" in report, report
    print("check_code tool: bundled pyflakes flags the undefined name  ✓")

    # ── SECURITY: a trojan binary shipped in the repo's .venv is NEVER run ──
    # A hostile clone can commit .venv/bin/pyright; check_code is risk="safe"
    # (auto-run by goal verify), so resolving from workdir/.venv would be RCE.
    import os
    import stat

    jail = Path(tempfile.mkdtemp(prefix="rockyjail-")).resolve()
    (jail / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    venv_bin = jail / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    trojan = venv_bin / "pyright"
    trojan.write_text("#!/bin/sh\ntouch PWNED\n")  # would drop a sentinel if run
    trojan.chmod(trojan.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # nothing genuine on PATH → old code fell through to .venv; new code must not.
    checks.shutil.which = lambda c: None
    try:
        assert checks._resolve("pyright", jail) is None, "resolve must reject a .venv-only binary"
        det = checks.detect_checks(jail)
        assert not any(lbl == "pyright" for lbl, _ in det), f"trojan .venv/bin/pyright resolved! {det}"
        report = await checks.build_check_tool(jail)["check_code"].fn()
    finally:
        checks.shutil.which = real_which
    assert not (jail / "PWNED").exists(), "TROJAN EXECUTED — .venv binary ran"
    print("checks._resolve: trojan .venv/bin/pyright is not resolved or run  ✓")

    # A PATH hit that resolves UNDER workdir (activated venv / `.` on PATH) is also refused.
    checks.shutil.which = lambda c: str(jail / ".venv" / "bin" / c)
    try:
        assert checks._resolve("pyright", jail) is None, "under-workdir PATH hit not refused"
    finally:
        checks.shutil.which = real_which
    # …but a genuine PATH hit outside workdir is accepted.
    checks.shutil.which = lambda c: "/usr/bin/definitely-not-in-workdir"
    try:
        assert checks._resolve("ruff", jail) == "/usr/bin/definitely-not-in-workdir"
    finally:
        checks.shutil.which = real_which
    print("checks._resolve: PATH-only, under-workdir refused, outside accepted  ✓")

    shutil.rmtree(jail, ignore_errors=True)
    print("CHECKS SMOKE OK — amaze amaze amaze!")


asyncio.run(main())
