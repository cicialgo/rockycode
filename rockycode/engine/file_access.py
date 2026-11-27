"""Host-owned file scope, independent of approval UI and model tool arguments.

Bounded by default. Only a trusted local host may supply a live mode getter to
open ordinary reads in YOLO. This does not grant writes or isolate processes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, MutableSet

READ_TOOLS = ("read_file", "grep", "glob", "view_image")


def resolve_path(path: str, workdir: Path) -> Path:
    target = Path(path).expanduser()
    return (target if target.is_absolute() else workdir / target).resolve()


def read_target(tool: str, args: dict, workdir: Path) -> Path | None:
    """Root requested by a read; glob uses its literal prefix, not its results.

    Every traversal target is checked separately. A glob rooted inside the
    workspace cannot acquire access to an outside symlink just by matching it.
    """
    if tool not in READ_TOOLS:
        return None
    value = args.get("pattern") if tool == "glob" else args.get("path", "." if tool == "grep" else None)
    if not isinstance(value, str) or not value:
        return None
    if tool == "glob":
        parts = Path(value).expanduser().parts
        prefix = []
        for part in parts:
            if any(char in part for char in "*?["):
                break
            prefix.append(part)
        value = str(Path(*prefix)) if prefix else "."
    return resolve_path(value, workdir)


@dataclass
class FileAccessPolicy:
    workdir: Path
    allowed_roots: tuple[Path, ...] = ()
    read_grants: MutableSet[Path] = field(default_factory=set)
    local_mode: Callable[[], str] | None = None

    def resolve(self, path: str, *, read: bool = False) -> tuple[Path | None, str | None]:
        try:
            target = resolve_path(path, self.workdir)
            roots = (self.workdir.resolve(), *self.allowed_roots, *(self.read_grants if read else ()))
            if read and self.local_mode is not None and self.local_mode() == "yolo":
                return target, None
            if any(target == root or root in target.parents for root in roots):
                return target, None
        except (OSError, ValueError, RuntimeError) as exc:
            return None, f"[error] invalid file path: {exc}"
        return None, f"[blocked] path escapes the working directory and authorized roots: {path}"

    def grant_read(self, tool: str, args: dict) -> None:
        """Retain the existing session read-grant contract for all read tools.

        Called only by host approval handling, never by model-facing tools.
        One-shot versus session grant lifetime is a separate follow-up.
        """
        try:
            target = read_target(tool, args, self.workdir)
        except (OSError, ValueError, RuntimeError):
            return
        if target is not None:
            self.read_grants.add(target)
