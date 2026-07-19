"""Run engine tools inside a SWE-bench task container.

A *session* is anything with `exec(script, stdin, timeout)`. DockerSession
talks to a long-lived container via `docker exec`; LocalSession runs the
same scripts in a local directory so the glue is testable without Docker.

SWE-bench image layout (constant across the official images):
  repo checked out at /testbed at the task's base_commit
  conda env "testbed" at /opt/miniconda3 with the repo's deps installed
"""
from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from typing import Optional

from rockycode.engine.tools import (
    GLOB_MAX_PATHS,
    GREP_MAX_MATCHES,
    RISK,
    SCHEMAS,
    Tool,
    _truncate,
)

# Generous: real test suites run under emulation on Apple Silicon.
EXEC_TIMEOUT_S = 300

# Activate the task env and land in the repo before every command.
_WRAP = "source /opt/miniconda3/bin/activate testbed 2>/dev/null; cd /testbed 2>/dev/null; "

# `git add -A` respects .gitignore, so build junk stays out; --cached diff
# then captures edits AND new files the agent created.
GIT_DIFF_SCRIPT = "git add -A >/dev/null 2>&1; git -c core.fileMode=false diff --cached"


class DockerSession:
    """One long-lived container per task; commands go through docker exec."""

    def __init__(self, container_id: str) -> None:
        self.container_id = container_id

    @classmethod
    async def start(cls, image: str, *, platform: str = "linux/amd64") -> "DockerSession":
        proc = await asyncio.create_subprocess_exec(
            "docker", "run", "-d", "--platform", platform, image, "tail", "-f", "/dev/null",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"docker run failed for {image}: {err.decode(errors='replace').strip()}")
        return cls(out.decode().strip())

    async def exec(
        self,
        script: str,
        *,
        stdin: Optional[bytes] = None,
        timeout: int = EXEC_TIMEOUT_S,
    ) -> tuple[str, int]:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", self.container_id, "bash", "-c", _WRAP + script,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(input=stdin), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            # NB: kills the host-side `docker exec`; the in-container process
            # may linger. Acceptable for v1 — the container dies after the task.
            return f"[timeout] command exceeded {timeout}s and was killed", 124
        return out.decode(errors="replace"), proc.returncode or 0

    async def stop(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", self.container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()


class LocalSession:
    """Same interface, local bash in a directory. For tests."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir

    async def exec(
        self,
        script: str,
        *,
        stdin: Optional[bytes] = None,
        timeout: int = EXEC_TIMEOUT_S,
    ) -> tuple[str, int]:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", script,
            cwd=self.workdir,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(input=stdin), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return f"[timeout] command exceeded {timeout}s and was killed", 124
        return out.decode(errors="replace"), proc.returncode or 0

    async def stop(self) -> None:
        pass


async def _bash(session, command: str) -> str:
    out, code = await session.exec(command)
    if out.startswith("[timeout]"):
        return out
    return _truncate(f"[exit {code}]\n{out}" if out else f"[exit {code}]")


async def _read_file(session, path: str, offset=None, limit=None) -> str:
    q = shlex.quote(path)
    # cat -n BEFORE sed: numbering happens on the whole file, so a windowed
    # read shows absolute line numbers the model can feed straight to edits.
    window = ""
    if offset or limit:
        start = max(int(offset or 1), 1)
        end = str(start + int(limit) - 1) if limit else "$"
        window = f" | sed -n '{start},{end}p'"
    out, _code = await session.exec(
        f'if [ -d {q} ]; then echo "[directory] {path}"; ls -p {q}; '
        f'elif [ -e {q} ]; then cat -n {q}{window}; '
        f'else echo "[error] file not found: {path}"; exit 1; fi'
    )
    return _truncate(out.rstrip("\n"))


async def _write_file(session, path: str, content: str) -> str:
    q = shlex.quote(path)
    out, code = await session.exec(
        f'mkdir -p "$(dirname {q})" && cat > {q}', stdin=content.encode()
    )
    if code != 0:
        return f"[error] write failed: {out.strip()}"
    return f"[ok] wrote {len(content)} chars to {path}"


async def _edit_file(session, path: str, old_string: str, new_string: str) -> str:
    q = shlex.quote(path)
    text, code = await session.exec(f"cat {q}")
    if code != 0:
        return f"[error] file not found: {path}"
    n = text.count(old_string)
    if n == 0:
        return "[error] old_string not found in file. read the file again — it may have changed."
    if n > 1:
        return f"[error] old_string appears {n} times; include more surrounding context to make it unique."
    return await _write_file(session, path, text.replace(old_string, new_string, 1))


async def _grep(session, pattern: str, path: str = ".", include: str | None = None) -> str:
    inc = f" --include={shlex.quote(include)}" if include else ""
    # -I skips binaries; junk dirs excluded to match the local tool's behavior.
    # No pipe here: it would mask grep's exit code (1 = no match, 2 = bad
    # pattern); matches are capped host-side instead.
    excludes = " ".join(f"--exclude-dir={d}" for d in (".git", "__pycache__", ".venv", "node_modules"))
    cmd = f"grep -rnIE {excludes}{inc} -e {shlex.quote(pattern)} {shlex.quote(path)}"
    out, code = await session.exec(cmd)
    out = out.rstrip("\n")
    if code == 1:
        return "[no matches]"
    if code != 0:
        return f"[error] grep failed (bad pattern or path): {out[:200]}"
    lines = out.splitlines()
    if len(lines) > GREP_MAX_MATCHES:
        lines = lines[:GREP_MAX_MATCHES]
        lines.append(f"[truncated at {GREP_MAX_MATCHES} matches — narrow the pattern]")
    return _truncate("\n".join(lines))


_GLOB_PY = (
    "import glob, sys; "
    f"hits = sorted(glob.glob(sys.argv[1], recursive=True))[:{GLOB_MAX_PATHS}]; "
    "print('\\n'.join(hits) if hits else '[no matches]')"
)


async def _glob(session, pattern: str) -> str:
    out, code = await session.exec(f"python -c {shlex.quote(_GLOB_PY)} {shlex.quote(pattern)}")
    if code != 0:
        return f"[error] glob failed: {out.strip()[:200]}"
    return _truncate(out.rstrip("\n"))


def build_session_registry(session) -> dict[str, Tool]:
    """The same tools the chat TUI has, executing inside the session."""
    fns = {
        "bash": lambda command: _bash(session, command),
        "read_file": lambda path, offset=None, limit=None: _read_file(session, path, offset, limit),
        "write_file": lambda path, content: _write_file(session, path, content),
        "edit_file": lambda path, old_string, new_string: _edit_file(
            session, path, old_string, new_string
        ),
        "grep": lambda pattern, path=".", include=None: _grep(session, pattern, path, include),
        "glob": lambda pattern: _glob(session, pattern),
    }
    return {
        name: Tool(name=name, schema=SCHEMAS[name], fn=fn, risk=RISK.get(name, "risky"))
        for name, fn in fns.items()
    }


async def extract_patch(session) -> str:
    """The agent's work as a git patch — this is the SWE-bench prediction."""
    out, code = await session.exec(GIT_DIFF_SCRIPT)
    if code != 0:
        return ""
    patch = out
    if patch and not patch.endswith("\n"):
        patch += "\n"
    return patch
