"""Persistent prompt history for the chat input — the up-arrow list survives
restarts (ported from a research branch, persistence only: draft
handling and the visual-row navigation stay in ChatInput, which already does
them better).

ONE global file, like shell history — renaming or moving a project never loses
it, and prompts can't end up committed inside a repo's tree. Honors
$ROCKYCODE_HOME, same convention as session.py / trajectory.py.

Storage is append-only JSONL ({"input": ...} per line, the research branch's
format), adjacent-deduped, capped at LIMIT by a full rewrite once over. All IO
is best-effort: a broken disk or a torn line must never break typing.

A key pasted into the chat input must not be kept: every line is stored with
key-shaped text masked (engine/redact.py), a file from before is cleaned the
next time it is loaded, and the file is the user's alone (0600).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from rockycode.engine.redact import redact_typed


def default_path() -> Path:
    home = Path(os.environ.get("ROCKYCODE_HOME") or Path.home() / ".rockycode")
    return home / "prompt-history.jsonl"


class PromptHistory:
    """Load-on-start, append-on-submit. `items` is oldest-first, ready to seed
    ChatInput._history directly."""

    LIMIT = 200

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()
        self.items: list[str] = []
        self._load()

    def _load(self) -> None:
        try:
            lines = self.path.read_text().splitlines()
        except OSError:
            return
        items: list[str] = []
        dirty = False
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn write / hand edit — skip the line, keep the rest
            text = value.get("input") if isinstance(value, dict) else value
            if isinstance(text, str) and text.strip():
                kept = redact_typed(text)
                dirty = dirty or kept != text
                if not items or items[-1] != kept:
                    items.append(kept)
        self.items = items[-self.LIMIT:]
        try:
            os.chmod(self.path, 0o600)  # a file from before was created world-readable
        except OSError:
            pass
        if dirty:  # a key typed before this version: off the disk now, not at the next cap
            self._rewrite()

    def _write(self, text: str, *, append: bool) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
            with os.fdopen(os.open(self.path, flags, 0o600), "w") as f:
                f.write(text)
        except OSError:
            pass

    def _rewrite(self) -> None:
        self._write("".join(json.dumps({"input": item}, ensure_ascii=False) + "\n" for item in self.items),
                    append=False)

    def append(self, text: str) -> None:
        text = redact_typed(text.strip())
        if not text or (self.items and self.items[-1] == text):
            return
        self.items.append(text)
        if len(self.items) > self.LIMIT:
            self.items = self.items[-self.LIMIT:]
            self._rewrite()
            return
        self._write(json.dumps({"input": text}, ensure_ascii=False) + "\n", append=True)
