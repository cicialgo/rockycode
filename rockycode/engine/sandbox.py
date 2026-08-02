"""Chat sandbox: a lightweight Docker container that runs the agent's tools
in isolation while sharing the project directory.

Same Session interface as container.DockerSession (exec / stop), so the
existing build_session_registry() works unchanged — only the container
internals differ (no conda, no /testbed, just /workspace).

Lifecycle: created on /sandbox on, destroyed on /sandbox off or app exit.

The default image is minimal (debian:bookworm-slim). Override via
ROCKYCODE_SANDBOX_IMAGE if Docker Hub is unreachable.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
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

# python-preinstalled so the agent can run/test code offline and an approved
# `pip install` doesn't have to apt-bootstrap the toolchain (the old .deb pile).
# Override with ROCKYCODE_SANDBOX_IMAGE for non-python projects.
DEFAULT_SANDBOX_IMAGE = "python:3.12-slim"
EXEC_TIMEOUT_S = 120

# docker exec has no host-side API for killing one running exec. Run each tool
# command under a small Python supervisor inside the container: the child bash
# gets its own process group and its group leader is written to a per-call file.
# A second docker exec can then terminate that exact group on timeout/cancel.
_EXEC_WRAPPER = """
import os, subprocess, sys
pidfile, script = sys.argv[1], sys.argv[2]
proc = subprocess.Popen(["bash", "-c", script], start_new_session=True)
with open(pidfile, "w", encoding="ascii") as f:
    f.write(str(proc.pid))
try:
    code = proc.wait()
finally:
    try:
        os.unlink(pidfile)
    except FileNotFoundError:
        pass
raise SystemExit(code)
"""

_KILL_WRAPPER = """
import os, signal, sys, time
pidfile = sys.argv[1]
pid = None
for _ in range(20):
    try:
        with open(pidfile, encoding="ascii") as f:
            pid = int(f.read().strip())
        break
    except (FileNotFoundError, ValueError):
        time.sleep(0.05)
if pid is not None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            break
        if sig == signal.SIGTERM:
            time.sleep(0.2)
try:
    os.unlink(pidfile)
except FileNotFoundError:
    pass
"""


class ChatSandbox:
    """One lightweight container per chat session; project dir mounted at /workspace."""

    def __init__(self, container_id: str, workdir: Path) -> None:
        self.container_id = container_id
        self.workdir = workdir
        self._running = True
        self._has_py3 = True  # start() probes; direct construction assumes the default image

    @classmethod
    async def start(cls, workdir: Path, *, image: str | None = None,
                    network: bool = False) -> "ChatSandbox":
        img = image or os.getenv("ROCKYCODE_SANDBOX_IMAGE", DEFAULT_SANDBOX_IMAGE)
        wd = workdir.resolve()
        # network=False → `--network none`: no egress at all. This is the SAFE
        # DEFAULT for every surface (no exfiltration, no apt/pip rabbit-holes);
        # the container can only touch the mounted workspace. Callers that need
        # the network (goal mode when the plan implies it, chat/exec on request)
        # pass network=True explicitly and say so out loud.
        net_args = [] if network else ["--network", "none"]
        proc = await asyncio.create_subprocess_exec(
            "docker", "run", "-d", "--rm",
            *net_args,
            "-v", f"{wd}:/workspace:rw",
            "-w", "/workspace",
            img, "tail", "-f", "/dev/null",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            msg = err.decode(errors="replace").strip()
            hint = ""
            if "pull access denied" in msg.lower() or "not found" in msg.lower():
                hint = (
                    f"\n  [hint] this image may not be pulled yet. "
                    f"try: docker pull {img}"
                )
            elif "Cannot connect" in msg or "Is the docker daemon" in msg:
                hint = "\n  [hint] docker daemon is not running or not reachable."
            raise RuntimeError(f"sandbox start failed for {img}: {msg}{hint}")
        sandbox = cls(out.decode().strip(), wd)
        # The cancel/timeout supervisor runs under python3 inside the container.
        # A custom ROCKYCODE_SANDBOX_IMAGE without it (node, go, …) falls back
        # to plain bash -c: commands still run, but cancel can only kill the
        # host-side docker client, not the in-container process tree.
        probe = await asyncio.create_subprocess_exec(
            "docker", "exec", sandbox.container_id, "python3", "-c", "",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        sandbox._has_py3 = await probe.wait() == 0
        return sandbox

    async def exec(
        self,
        script: str,
        *,
        stdin: Optional[bytes] = None,
        timeout: int = EXEC_TIMEOUT_S,
    ) -> tuple[str, int]:
        if not self._running:
            return "[error] sandbox has been stopped", 1
        pidfile = f"/tmp/rockycode-exec-{secrets.token_hex(12)}.pid" if self._has_py3 else None
        cmd = (["python3", "-c", _EXEC_WRAPPER, pidfile, script] if pidfile
               else ["bash", "-c", script])
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", self.container_id, *cmd,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(input=stdin), timeout=timeout)
        except asyncio.TimeoutError:
            await self._terminate_exec(proc, pidfile)
            return f"[timeout] command exceeded {timeout}s and was killed", 124
        except asyncio.CancelledError:
            # Esc/new-submit cancellation must not leave a command running in the
            # container against the read-write /workspace mount.
            await self._terminate_exec(proc, pidfile)
            raise
        return out.decode(errors="replace"), proc.returncode or 0

    async def _terminate_exec(self, proc, pidfile: str | None) -> None:
        """Kill one supervised in-container command, then reap docker exec."""
        try:
            if self._running and pidfile is not None:
                try:
                    killer = await asyncio.create_subprocess_exec(
                        "docker", "exec", self.container_id,
                        "python3", "-c", _KILL_WRAPPER, pidfile,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    try:
                        await asyncio.wait_for(killer.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            killer.kill()
                        await killer.wait()
                except OSError:
                    # Docker may disappear during app shutdown. Preserve the
                    # caller's TimeoutError/CancelledError and still reap the
                    # host-side client below.
                    pass
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()  # reap a stuck host-side docker client too
            await proc.wait()

    async def stop(self) -> None:
        self._running = False
        proc = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", self.container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

    @property
    def is_running(self) -> bool:
        return self._running


# ── tool wrappers (same logic as container.py, minus conda / SWE-bench paths) ──

async def _bash(sandbox: ChatSandbox, command: str) -> str:
    out, code = await sandbox.exec(command)
    if out.startswith("[timeout]"):
        return out
    return _truncate(f"[exit {code}]\n{out}" if out else f"[exit {code}]")


async def _read_file(sandbox: ChatSandbox, path: str, offset=None, limit=None) -> str:
    q = shlex.quote(path)
    # cat -n before sed: absolute line numbers survive windowed reads (same
    # contract as the local and bench-container read_file).
    window = ""
    if offset or limit:
        start = max(int(offset or 1), 1)
        end = str(start + int(limit) - 1) if limit else "$"
        window = f" | sed -n '{start},{end}p'"
    out, _code = await sandbox.exec(
        f'if [ -d {q} ]; then echo "[directory] {path}"; ls -p {q}; '
        f'elif [ -e {q} ]; then cat -n {q}{window}; '
        f'else echo "[error] file not found: {path}"; exit 1; fi'
    )
    return _truncate(out.rstrip("\n"))


async def _write_file(sandbox: ChatSandbox, path: str, content: str) -> str:
    q = shlex.quote(path)
    out, code = await sandbox.exec(
        f'mkdir -p "$(dirname {q})" && cat > {q}', stdin=content.encode()
    )
    if code != 0:
        return f"[error] write failed: {out.strip()}"
    return f"[ok] wrote {len(content)} chars to {path}"


async def _edit_file(sandbox: ChatSandbox, path: str, old_string: str, new_string: str) -> str:
    q = shlex.quote(path)
    text, code = await sandbox.exec(f"cat {q}")
    if code != 0:
        return f"[error] file not found: {path}"
    n = text.count(old_string)
    if n == 0:
        return "[error] old_string not found in file. read the file again — it may have changed."
    if n > 1:
        return f"[error] old_string appears {n} times; include more surrounding context to make it unique."
    return await _write_file(sandbox, path, text.replace(old_string, new_string, 1))


async def _grep(sandbox: ChatSandbox, pattern: str, path: str = ".", include: str | None = None) -> str:
    inc = f" --include={shlex.quote(include)}" if include else ""
    excludes = " ".join(f"--exclude-dir={d}" for d in (".git", "__pycache__", ".venv", "node_modules"))
    cmd = f"grep -rnIE {excludes}{inc} -e {shlex.quote(pattern)} {shlex.quote(path)}"
    out, code = await sandbox.exec(cmd)
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


async def _glob(sandbox: ChatSandbox, pattern: str) -> str:
    out, code = await sandbox.exec(f"python3 -c {shlex.quote(_GLOB_PY)} {shlex.quote(pattern)}")
    if code != 0:
        return f"[error] glob failed: {out.strip()[:200]}"
    return _truncate(out.rstrip("\n"))


def build_sandbox_registry(sandbox: ChatSandbox, *, extras: dict[str, Tool] | None = None) -> dict[str, Tool]:
    """Build a tool registry bound to a chat sandbox (same schema, capsuled execution).

    extras are merged on top — e.g. web_search/web_research/web_fetch that run on the
    host, not inside the container.
    """
    fns = {
        "bash": lambda command: _bash(sandbox, command),
        "read_file": lambda path, offset=None, limit=None: _read_file(sandbox, path, offset, limit),
        "write_file": lambda path, content: _write_file(sandbox, path, content),
        "edit_file": lambda path, old_string, new_string: _edit_file(
            sandbox, path, old_string, new_string
        ),
        "grep": lambda pattern, path=".", include=None: _grep(sandbox, pattern, path, include),
        "glob": lambda pattern: _glob(sandbox, pattern),
    }
    # Same risk tiers as the host registry (read_file/grep/glob = "safe") so the
    # loop's read-parallelism kicks in inside the sandbox too — goal mode and
    # `chat --sandbox` both build their registry here. Without this the reads
    # default to "risky" and every explore batch runs serially.
    reg = {name: Tool(name=name, schema=SCHEMAS[name], fn=fn, risk=RISK.get(name, "risky"))
           for name, fn in fns.items()}
    if extras:
        reg.update(extras)
    return reg
