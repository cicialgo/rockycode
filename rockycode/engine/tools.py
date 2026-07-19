"""v1 tool set: bash, read_file, write_file, edit_file, grep, glob.

Small and orthogonal on purpose. Each tool is an async function plus an
OpenAI-shape schema; the registry is what the loop hands to the model.
Output is truncated before it reaches the model so one noisy command can't
blow the context.

grep/glob exist even though bash could do both: dedicated tools with tight
schemas are easier for smaller models to call correctly, and their outputs
are capped + cleaned (no junk dirs, no binary files).
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from rockycode.engine.redact import redact

MAX_OUTPUT_CHARS = 30_000
# Seconds before a bash command is killed. Raise it for long jobs (e.g. many
# file downloads) via ROCKYCODE_BASH_TIMEOUT.
BASH_TIMEOUT_S = int(os.environ.get("ROCKYCODE_BASH_TIMEOUT", "300"))
GREP_MAX_MATCHES = 200
GLOB_MAX_PATHS = 500

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".rockycode", ".cache"}


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    dropped = len(text) - limit
    return f"{head}\n... [{dropped} chars truncated] ...\n{tail}"


@dataclass
class Tool:
    name: str
    schema: dict
    fn: Callable[..., Awaitable[str]]
    # Approval tier for the permission layer. "risky" is the fail-safe default so a
    # tool that forgets to classify itself (incl. dynamic MCP tools) gets gated
    # rather than silently allowed. See engine/permission.py for tier -> policy.
    risk: str = "risky"


def _fn_schema(name: str, description: str, params: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": params, "required": required},
        },
    }


# Shared by the local registry (chat) and the container registry (bench) —
# the model sees identical tools either way, only the execution target differs.
SCHEMAS: dict[str, dict] = {
    "bash": _fn_schema(
        "bash",
        "Run a shell command in the repository working directory. "
        "Stdout+stderr combined, exit code on the first line.",
        {"command": {"type": "string", "description": "The shell command to run."}},
        ["command"],
    ),
    # offset/limit exist because the model keeps asking for them: 128 chat +
    # ~60 bench "[error] … unexpected keyword argument 'offset'" trajectories
    # show DeepSeek's prior wants windowed reads. Line numbers stay ABSOLUTE
    # (numbered before slicing) so follow-up edits reference real positions.
    "read_file": _fn_schema(
        "read_file",
        "Read a file (line-numbered) or list a directory.",
        {
            "path": {"type": "string", "description": "File or directory path."},
            "offset": {"type": "integer", "description": "1-based line number to start from (optional)."},
            "limit": {"type": "integer", "description": "Max lines to return from offset (optional)."},
        },
        ["path"],
    ),
    "write_file": _fn_schema(
        "write_file",
        "Create or overwrite a file with the given content.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"],
    ),
    "edit_file": _fn_schema(
        "edit_file",
        "Replace one exact occurrence of old_string with new_string in a file. "
        "old_string must be unique in the file — include surrounding context.",
        {
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
        },
        ["path", "old_string", "new_string"],
    ),
    "grep": _fn_schema(
        "grep",
        "Search file contents with an extended regular expression. Returns up to "
        f"{GREP_MAX_MATCHES} matches as path:line: text. Prefer this over `grep` in bash — "
        "it skips binaries and junk directories.",
        {
            "pattern": {"type": "string", "description": "Extended regex to search for."},
            "path": {"type": "string", "description": "Directory to search. Default: working dir."},
            "include": {
                "type": "string",
                "description": "Only search files whose name matches this glob, e.g. '*.py'.",
            },
        },
        ["pattern"],
    ),
    "glob": _fn_schema(
        "glob",
        "List files matching a path pattern, relative to the working directory. "
        "Supports ** for recursion, e.g. '**/*.py' or 'lib/**/test_*.py'. "
        f"Returns up to {GLOB_MAX_PATHS} sorted paths.",
        {
            "pattern": {"type": "string", "description": "Path glob, ** allowed."},
        },
        ["pattern"],
    ),
}


async def _bash(command: str, *, workdir: Path) -> str:
    # start_new_session=True gives the shell its own process group, so on a
    # timeout we can kill the whole tree (curl/wget/… children) — not just the
    # shell, which proc.kill() alone would leave running as orphans.
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=workdir,
        start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=BASH_TIMEOUT_S)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # whole group (pgid == shell pid)
        except ProcessLookupError:
            pass
        try:
            await proc.wait()  # reap the shell so it doesn't linger as a zombie
        except Exception:  # noqa: BLE001
            pass
        # No retry: re-running as-is would just time out again. Tell the model/
        # user what happened and how to recover.
        return (
            f"[timeout] command exceeded {BASH_TIMEOUT_S}s and was killed "
            f"(whole process group terminated). For a longer command, raise "
            f"ROCKYCODE_BASH_TIMEOUT; or split the work into smaller steps."
        )
    except asyncio.CancelledError:
        # User interrupted (Esc / new message). asyncio.wait_for cancels
        # communicate() but that does NOT kill the OS process — SIGKILL the whole
        # group so the shell and its curl/build/… children don't survive as
        # orphans (the reason for start_new_session above). Then re-raise: the
        # loop's cancellation backfill relies on CancelledError propagating.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        raise
    text = out.decode(errors="replace")
    status = f"[exit {proc.returncode}]"
    return _truncate(f"{status}\n{text}" if text else status)


# Filenames that almost always hold secrets. Reading them just funnels
# credentials into history/the trajectory, so read_file refuses outright (any
# mode) — the model should ask the user for a specific value it needs.
_SECRET_FILE = re.compile(
    r"(?i)(^|/)("
    r"\.env(\.[\w.-]+)?|\.netrc|\.npmrc|\.pypirc|\.git-credentials|"
    r"id_(rsa|dsa|ecdsa|ed25519)|[\w.-]+\.pem|[\w.-]+\.key"
    r")$"
)


def _is_secret_file(p: Path) -> bool:
    s = str(p)
    if "/.ssh/" in s or "/.aws/credentials" in s or "/.gnupg/" in s:
        return True
    return bool(_SECRET_FILE.search("/" + p.name))


def _jail(
    path: str, workdir: Path, allowed_roots: tuple[Path, ...] = (), grants=(),
) -> tuple[Optional[Path], Optional[str]]:
    """Resolve *path* and confine it to *workdir* (plus any *allowed_roots*).
    Returns (resolved_path, None) on success or (None, error_string) if it escapes.

    *grants* are resolved paths a live human approval widened the jail to — READS
    ONLY (write/edit never pass them). Like allowed_roots they can't come from a
    file, only a launch flag or an in-session approval click, so an untrusted repo
    can't grant itself out.

    This is a HARD jail inside the tool, not the advisory permission layer:
    bench/serve/yolo all bypass that layer, so read/write/edit must refuse an
    escape on their own. resolve() collapses `..` and follows symlink components
    (a symlink inside workdir that points out, or an absolute path, is caught).
    strict=False so a not-yet-created write target still resolves. Best-effort
    against a symlink swapped in after the check (a local race, out of scope for
    the hostile-model threat model); it fully blocks model-driven `..`/abs/symlink
    escapes. Reads/writes outside these roots must go through the (gated) bash tool.

    *allowed_roots* are extra in-bounds directories the HUMAN declared at launch
    (`--allow-dir`), already resolved. Deliberately NOT sourced from a project's
    `.rockycode/config.toml` — an untrusted repo must never be able to widen its
    own jail; only an explicit launch-time flag can.
    """
    p = Path(path)
    if not p.is_absolute():
        p = workdir / p
    try:
        resolved = p.resolve()
        wd = workdir.resolve()
    except OSError as e:
        return None, f"[error] {e}"
    roots = (wd, *allowed_roots, *grants)
    if any(resolved == r or r in resolved.parents for r in roots):
        return resolved, None
    extra = (
        f" or a declared --allow-dir root ({', '.join(str(r) for r in allowed_roots)})"
        if allowed_roots else ""
    )
    return None, (
        f"[blocked] path escapes the working directory: {path}. "
        f"read/write/edit are confined to {wd}{extra}."
    )


async def _read_file(path: str, *, workdir: Path, allowed_roots: tuple[Path, ...] = (), read_grants=None,
                     offset=None, limit=None) -> str:
    p, err = _jail(path, workdir, allowed_roots, grants=tuple(read_grants or ()))
    if err:
        return err
    if _is_secret_file(p):
        return (
            f"[blocked] refusing to read '{p.name}' — it likely holds secrets "
            f"(.env / credentials / private key). Ask the user for the specific "
            f"value you need instead of reading the file."
        )
    if not p.exists():
        return f"[error] file not found: {p}"
    if p.is_dir():
        entries = "\n".join(sorted(e.name + ("/" if e.is_dir() else "") for e in p.iterdir()))
        return _truncate(f"[directory] {p}\n{entries}")
    try:
        text = p.read_text(errors="replace")
    except OSError as e:
        return f"[error] {e}"
    lines = text.splitlines()
    start = max(int(offset) - 1, 0) if offset else 0
    if start >= len(lines) > 0:
        return f"[error] offset {offset} is past the end of the file ({len(lines)} lines)"
    end = start + int(limit) if limit else len(lines)
    numbered = "\n".join(f"{i + 1}\t{lines[i]}" for i in range(start, min(end, len(lines))))
    return _truncate(numbered)


def _secret_block(p: Path) -> Optional[str]:
    """Error string if writing/editing *p* would clobber a likely-secret file,
    else None. Symmetric with read_file's refusal — a model shouldn't be able to
    overwrite ~/.ssh keys, .env, .npmrc, etc. any more than it can read them."""
    if _is_secret_file(p):
        return (
            f"[blocked] refusing to write '{p.name}' — it looks like a secrets / "
            f"credentials file (.env / key / .ssh / .aws). If you truly need this, "
            f"ask the user to do it."
        )
    return None


async def _write_file(path: str, content: str, *, workdir: Path, allowed_roots: tuple[Path, ...] = ()) -> str:
    p, err = _jail(path, workdir, allowed_roots)
    if err:
        return err
    if (blocked := _secret_block(p)) is not None:
        return blocked
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    except OSError as e:
        return f"[error] {e}"
    return f"[ok] wrote {len(content)} chars to {p}"


async def _edit_file(path: str, old_string: str, new_string: str, *, workdir: Path, allowed_roots: tuple[Path, ...] = ()) -> str:
    p, err = _jail(path, workdir, allowed_roots)
    if err:
        return err
    if (blocked := _secret_block(p)) is not None:
        return blocked
    if not p.exists():
        return f"[error] file not found: {p}"
    try:
        raw = p.read_bytes()
    except OSError as e:
        return f"[error] {e}"
    if b"\0" in raw[:8192]:  # don't corrupt binaries with a text round-trip
        return f"[error] {p} looks binary — refusing to edit."
    text = raw.decode(errors="replace")
    n = text.count(old_string)
    if n == 0:
        return "[error] old_string not found in file. read the file again — it may have changed."
    if n > 1:
        return f"[error] old_string appears {n} times; include more surrounding context to make it unique."
    try:
        p.write_text(text.replace(old_string, new_string, 1))
    except OSError as e:
        return f"[error] {e}"
    return f"[ok] edited {p}"


def _walk_files(root: Path):
    """Files under root, sorted, skipping junk dirs."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir(), reverse=True)
        except OSError:
            continue
        for e in entries:
            if e.is_dir():
                if e.name not in SKIP_DIRS:
                    stack.append(e)
            elif e.is_file():
                yield e


def _grep_sync(pattern: str, path: str, include: Optional[str], workdir: Path) -> str:
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"[error] bad regex: {e}"
    root = Path(path) if Path(path).is_absolute() else workdir / path
    if not root.exists():
        return f"[error] path not found: {root}"
    matches: list[str] = []
    for f in _walk_files(root):
        if include and not fnmatch.fnmatch(f.name, include):
            continue
        try:
            # one read: the handle is closed (no fd leak across many files),
            # binary detection and decode share the same bytes (no double read)
            raw = f.read_bytes()
            if b"\0" in raw[:8192]:
                continue  # binary
            text = raw.decode(errors="replace")
        except OSError:
            continue
        rel = f.relative_to(root)
        for i, line in enumerate(text.splitlines()):
            if rx.search(line):
                matches.append(f"{rel}:{i + 1}: {line.strip()[:200]}")
                if len(matches) >= GREP_MAX_MATCHES:
                    matches.append(f"[truncated at {GREP_MAX_MATCHES} matches — narrow the pattern]")
                    return "\n".join(matches)
    return "\n".join(matches) if matches else "[no matches]"


def _glob_sync(pattern: str, workdir: Path) -> str:
    try:
        hits = sorted(
            str(p.relative_to(workdir))
            for p in workdir.glob(pattern)
            if not any(part in SKIP_DIRS for part in p.parts)
        )
    except (ValueError, OSError) as e:
        return f"[error] bad pattern: {e}"
    if not hits:
        return "[no matches]"
    out = hits[:GLOB_MAX_PATHS]
    if len(hits) > GLOB_MAX_PATHS:
        out.append(f"[truncated: {len(hits)} total — narrow the pattern]")
    return "\n".join(out)


async def _grep(pattern: str, path: str = ".", include: Optional[str] = None, *, workdir: Path) -> str:
    return _truncate(await asyncio.to_thread(_grep_sync, pattern, path, include, workdir))


async def _glob(pattern: str, *, workdir: Path) -> str:
    return _truncate(await asyncio.to_thread(_glob_sync, pattern, workdir))


# Static risk tier per built-in tool, shared by build_registry and
# container.build_session_registry. Read-only = safe; filesystem mutation =
# moderate; arbitrary shell = risky. engine/permission.py maps (tier, mode) ->
# allow/ask. Anything not listed falls back to the Tool default ("risky").
RISK = {
    "read_file": "safe",
    "grep": "safe",
    "glob": "safe",
    "write_file": "moderate",
    "edit_file": "moderate",
    "bash": "risky",
}


def build_registry(workdir: Path, allowed_roots: tuple[Path, ...] = (), read_grants=None) -> dict[str, Tool]:
    """Tools bound to a local working directory (the chat TUI's registry).

    *allowed_roots* are extra in-bounds directories the human declared at launch
    (`--allow-dir`); read/write/edit accept paths inside them as well as workdir.
    *read_grants* is a live-mutable set of resolved paths a session approval
    widened the READ jail to (read_file only; never writes) — the read_file
    closure holds the set by reference, so approvals take effect immediately.
    """
    fns = {
        "bash": lambda command: _bash(command, workdir=workdir),
        "read_file": lambda path, offset=None, limit=None: _read_file(
            path, workdir=workdir, allowed_roots=allowed_roots, read_grants=read_grants,
            offset=offset, limit=limit),
        "write_file": lambda path, content: _write_file(path, content, workdir=workdir, allowed_roots=allowed_roots),
        "edit_file": lambda path, old_string, new_string: _edit_file(
            path, old_string, new_string, workdir=workdir, allowed_roots=allowed_roots
        ),
        "grep": lambda pattern, path=".", include=None: _grep(
            pattern, path, include, workdir=workdir
        ),
        "glob": lambda pattern: _glob(pattern, workdir=workdir),
    }
    reg = {
        name: Tool(name=name, schema=SCHEMAS[name], fn=fn, risk=RISK.get(name, "risky"))
        for name, fn in fns.items()
    }
    # check_code: run the project's own linters (see engine/checks.py). Lazy
    # import to avoid a tools<->checks import cycle. Chat registry only — bench
    # runs in the container registry, so published scores are unaffected.
    from rockycode.engine.checks import build_check_tool
    reg.update(build_check_tool(workdir))
    return reg


async def execute(registry: dict[str, Tool], name: str, arguments_json: str) -> tuple[str, bool]:
    """Run a tool by name with JSON-encoded args. Returns (output, ok).

    Never raises: malformed args / unknown tools / tool crashes all come back
    as error strings so the model can read them and recover.
    """
    tool = registry.get(name)
    if tool is None:
        return f"[error] unknown tool: {name}", False
    try:
        args = json.loads(arguments_json) if arguments_json.strip() else {}
    except json.JSONDecodeError as e:
        return f"[error] malformed tool arguments (invalid JSON): {e}", False
    if not isinstance(args, dict):
        return "[error] tool arguments must be a JSON object", False
    try:
        out = await tool.fn(**args)
        ok = not out.startswith(("[error]", "[timeout]"))
        # Redact secrets before the output enters history: history is what goes
        # to the API prompt AND the trajectory log, so masking here covers both,
        # and the model never sees a raw key it could echo later.
        return redact(out), ok
    except TypeError:
        # Name the accepted params instead of leaking the impl's lambda repr
        # ("build_session_registry.<locals>.<lambda>() got an unexpected …").
        props = tool.schema.get("function", {}).get("parameters", {}).get("properties", {})
        accepted = ", ".join(props) or "see the tool schema"
        return f"[error] bad arguments for {name} — accepted parameters: {accepted}", False
    except Exception as e:  # noqa: BLE001 — tool failures go back to the model
        return f"[error] {type(e).__name__}: {e}", False
