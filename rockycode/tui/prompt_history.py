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
"""
from __future__ import annotations

import json
import os
from pathlib import Path


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
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn write / hand edit — skip the line, keep the rest
            text = value.get("input") if isinstance(value, dict) else value
            if isinstance(text, str) and text.strip():
                if not items or items[-1] != text:
                    items.append(text)
        self.items = items[-self.LIMIT:]

    def _rewrite(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                "".join(json.dumps({"input": item}, ensure_ascii=False) + "\n"
                        for item in self.items))
        except OSError:
            pass

    def append(self, text: str) -> None:
        text = text.strip()
        if not text or (self.items and self.items[-1] == text):
            return
        self.items.append(text)
        if len(self.items) > self.LIMIT:
            self.items = self.items[-self.LIMIT:]
            self._rewrite()
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps({"input": text}, ensure_ascii=False) + "\n")
        except OSError:
            pass
