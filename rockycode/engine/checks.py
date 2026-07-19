"""Post-edit checks — run the project's OWN linter / type-checker and report.

The lightweight alternative to an LSP stack (see lsp.py): instead of a
language-server protocol, run the analyzers a human runs, auto-detected from the
repo. Same value ("did my change break lint/types?") at a fraction of the
machinery, and it matches what Claude Code does.

SAFETY: only DECLARATIVE analyzers are auto-run — ruff and tsc read config but
don't execute repo-authored code, and pyflakes has no config at all. We
deliberately do NOT auto-run mypy / eslint / `npm run lint`: their config can
load repo-authored plugins or scripts, which in a cloned repo is arbitrary code
execution (the same untrusted-clone vector we harden elsewhere). When no linter
is installed we fall back to BUNDLED pyflakes, so check_code always does
something useful on Python.

SAFETY (binary resolution): check-tool binaries are resolved from rocky's OWN
PATH only — NEVER from workdir/.venv. A cloned repo is untrusted; a trojan
`.venv/bin/pyright` there would be arbitrary code execution the moment
check_code (or goal-mode verify()) runs it — and check_code is risk="safe", so
it never prompts. Any resolved path that lands under workdir is refused too (an
activated project venv, or `.` on PATH). See _resolve.

  detect_checks(workdir)   -> [(label, argv)]
  run_checks(workdir)      -> str
  build_check_tool(workdir) -> {"check_code": Tool}   # the model-callable tool
"""
from __future__ import annotations

import asyncio
import importlib.util
import shutil
import sys
from pathlib import Path


def _pyflakes_available() -> bool:
    return importlib.util.find_spec("pyflakes") is not None


def _under(path: Path, root: Path) -> bool:
    """True if *path* is *root* or lives beneath it (both resolved)."""
    try:
        path, root = path.resolve(), root.resolve()
    except OSError:
        return True  # unresolvable → treat as unsafe (fail closed)
    return path == root or root in path.parents


def _resolve(tool: str, workdir: Path) -> str | None:
    """Find *tool* on rocky's OWN PATH — never inside the (untrusted) *workdir*.

    We deliberately do NOT look in workdir/.venv or any project-local dir: a
    cloned repo could ship a trojan `.venv/bin/pyright`, and check_code (risk=
    "safe", and auto-run by goal-mode verify()) would execute it with no prompt.
    So resolution is PATH-only, and a PATH hit that resolves to somewhere under
    workdir (an activated project venv, or `.` on PATH) is refused as well —
    check_code only ever runs a binary from rocky's own environment. A user who
    wants a project-only linter can put it on PATH themselves. (pyflakes is
    bundled with rocky and invoked via sys.executable, so Python always has a
    fallback that needs no external binary.)
    """
    found = shutil.which(tool)
    if not found:
        return None
    if _under(Path(found), workdir):
        return None  # binary lives under the untrusted repo — refuse it
    return found


def detect_checks(workdir: Path) -> list[tuple[str, list[str]]]:
    """Return [(label, argv)] of applicable SAFE fast checks for *workdir*.

    Only DECLARATIVE analyzers — ruff, pyright, tsc read config but never
    execute repo-authored code, and pyflakes has no config. (mypy/eslint/npm are
    excluded: their configs can load repo plugins/scripts = RCE in a cloned
    repo.) Binaries resolve from rocky's own PATH only, never workdir/.venv
    (see _resolve), so a trojan project-local binary is never executed.
    """
    checks: list[tuple[str, list[str]]] = []
    has_py = (workdir / "pyproject.toml").exists() or any(workdir.glob("*.py"))
    if has_py:
        ruff = _resolve("ruff", workdir)
        if ruff:
            checks.append(("ruff", [ruff, "check", "."]))
        elif _pyflakes_available():
            # Bundled fallback: pure-Python, no config, catches real bugs
            # (undefined names, unused imports) — the F-rules ruff also runs.
            checks.append(("pyflakes", [sys.executable, "-m", "pyflakes", "."]))
        pyright = _resolve("pyright", workdir)
        if pyright:  # declarative config, no plugin code-exec → safe to auto-run
            checks.append(("pyright", [pyright]))
    tsc = _resolve("tsc", workdir)
    if (workdir / "tsconfig.json").exists() and tsc:
        checks.append(("tsc", [tsc, "--noEmit"]))
    return checks


async def _run_one(argv: list[str], workdir: Path, timeout: int) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (FileNotFoundError, OSError) as e:
        return 127, f"could not run {argv[0]}: {e}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"{argv[0]} timed out after {timeout}s"
    return proc.returncode or 0, out.decode(errors="replace")


async def run_checks(workdir: Path, *, timeout: int = 120, tail: int = 15) -> str:
    """Run every detected check; return a concise report ('' if nothing applies).

    A non-zero exit shows the last *tail* lines so the model sees the concrete
    errors without the whole log.
    """
    checks = detect_checks(workdir)
    if not checks:
        return ""
    blocks: list[str] = []
    for label, argv in checks:
        code, out = await _run_one(argv, workdir, timeout)
        if code == 0:
            blocks.append(f"[ok] {label} — no issues")
        else:
            body = "\n  ".join(out.strip().splitlines()[-tail:])
            blocks.append(f"[FAILED] {label} (exit {code})\n  {body}")
    return "\n".join(blocks)


def build_check_tool(workdir: Path) -> dict:
    """The model-callable `check_code` tool: run the project's checks on demand."""
    from rockycode.engine.tools import Tool, _fn_schema

    async def check_code() -> str:
        report = await run_checks(workdir)
        return report or (
            "[no checks] no supported linter / type-checker for this project "
            "(ruff or bundled pyflakes for Python, tsc for TypeScript)."
        )

    schema = _fn_schema(
        "check_code",
        "Run the project's own linter / type-checker — ruff and pyright (or "
        "bundled pyflakes when ruff isn't installed) for Python, tsc for "
        "TypeScript — resolved from rocky's PATH, and report "
        "errors. Call it after editing to verify you didn't introduce lint or "
        "type errors — grounded feedback, not a guess. Read-only; project-wide.",
        {},
        [],
    )
    return {"check_code": Tool(name="check_code", schema=schema, fn=check_code, risk="safe")}
