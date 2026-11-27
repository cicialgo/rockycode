"""Trajectory logging in a training-ready format.

Every message the engine appends to its conversation history is mirrored to
a session JSONL file, OpenAI message shape, so a session can later become
SFT data or an RL rollout with zero retrofitting. This is the bridge from
"code tool" to "RL environment" — keep it append-only and boring.

Line shapes:
  {"t": <unix>, "kind": "meta",    "data": {model, thinking, ...}}
  {"t": <unix>, "kind": "message", "data": {role, content, ...}}      # as sent to / received from the API
  {"t": <unix>, "kind": "usage",   "data": {prompt_tokens, ...}}       # per API call
  {"t": <unix>, "kind": "compaction", "data": {strategy, ...}}         # history rewrite (see loop._maybe_compact)
  {"t": <unix>, "kind": "outcome", "data": {...}}                      # reward signal, when known
  {"t": <unix>, "kind": "reflect", "data": {seq, final, score, ...}}   # evolve checkpoint (see dream/reflect.py)
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path


def trajectory_dir() -> Path:
    """The one GLOBAL trajectory store. Honors $ROCKYCODE_HOME (tests and users
    redirect it); defaults to ~/.rockycode/trajectories.

    Every session lands here regardless of which project/cwd rocky is launched
    from. Each line's meta carries project_id/project_name/workdir, so readers
    (resume picker, dream) filter by project — deliberate global, not the old
    behaviour where a relative path dumped sessions wherever rocky was launched.
    """
    base = os.environ.get("ROCKYCODE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".rockycode"
    return root / "trajectories"


TRAJECTORY_DIR = trajectory_dir()


class TrajectoryLogger:
    def __init__(self, meta: dict, directory: Path | None = None, *, enabled: bool = True) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.session_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
        # Logging is best-effort: an unwritable location (read-only cwd/home,
        # full disk) must NOT crash the session at startup. On failure we set
        # path=None and every write below no-ops. disabled_reason lets a caller
        # surface it if it wants.
        self.path: Path | None = None
        self.disabled_reason: str | None = None
        if not enabled:
            # A host that owns persistence (the desktop model race) opts out up
            # front: same object, every write no-ops, nothing lands in ~/.rockycode.
            self.disabled_reason = "disabled by host"
            return
        if directory is None:
            directory = trajectory_dir()
            # Bench rollouts live in a subdir so they don't mix with chat
            # sessions — the resume picker and dream read only the top level.
            if meta.get("runner") == "rockycode" or meta.get("instance_id"):
                directory = directory / "bench"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self.path = directory / f"{self.session_id}.jsonl"
            self._write("meta", meta)
        except OSError as e:
            self.path = None
            self.disabled_reason = str(e)

    def _write(self, kind: str, data: dict) -> None:
        if self.path is None:
            return  # logging disabled (unwritable location) — silently skip
        line = json.dumps({"t": time.time(), "kind": kind, "data": data}, ensure_ascii=False)
        # encoding="utf-8" is required: ensure_ascii=False emits raw non-ASCII, and
        # the platform default (cp1252 on Windows) would raise UnicodeEncodeError on
        # the first emoji/CJK char and kill the session's logging.
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            # Went unwritable mid-session (disk full, unmounted) — stop quietly
            # rather than take the turn down with us.
            self.path = None
            self.disabled_reason = str(e)

    def message(self, msg: dict) -> None:
        self._write("message", msg)

    def reasoning(self, text: str) -> None:
        """Analysis/RL record retained for compatibility. Models requiring replay
        also store reasoning_content on their assistant message records."""
        if text:
            self._write("reasoning", {"text": text})

    def usage(self, usage: dict) -> None:
        if usage:
            self._write("usage", usage)

    def compaction(self, data: dict) -> None:
        self._write("compaction", data)

    def outcome(self, data: dict) -> None:
        self._write("outcome", data)

    def reflect(self, data: dict) -> None:
        # An evolve checkpoint (dream/reflect.py) — graded session state plus
        # pattern observations, written mid-session so a hard kill loses at
        # most the tail since the last checkpoint.
        self._write("reflect", data)

    def note(self, data: dict) -> None:
        self._write("note", data)

    def title(self, text: str) -> None:
        # Appended once the session has a name (see engine/titler.py); readers
        # take the LAST title record, so regenerating just appends a fresher one.
        if text and text.strip():
            self._write("title", {"title": text.strip()})


def append_record(path: Path, kind: str, data: dict) -> bool:
    """Append one record to an EXISTING trajectory file — for post-hoc writers
    like the dream's cold reflect, which grades straggler sessions long after
    their logger is gone. Same line shape and best-effort contract as
    TrajectoryLogger: returns False instead of raising."""
    try:
        line = json.dumps({"t": time.time(), "kind": kind, "data": data}, ensure_ascii=False)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except OSError:
        return False
