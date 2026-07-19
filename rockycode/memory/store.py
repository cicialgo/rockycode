"""Memory M0: plain markdown files, progressive disclosure, archive-not-delete.

Design: docs/memory-dream.md. The two rules that matter:

1. Markdown is the source of truth. One memory per file under
   .rockycode/memory/<type-dir>/, lenient `key: value` frontmatter (no YAML
   dependency, same parser style as skills.py). The user can read, edit,
   git-track, or delete any memory with normal tools; future indexes
   (sqlite-vec, M1) are rebuildable caches, never canonical.
2. Nothing is ever hard-deleted by rockycode. `rm` moves the file to
   archive/ with status flipped — wrong memories go away, but stay
   auditable.

Loading mirrors the skills pattern: MEMORY.md (the hand-curated index) and
`feedback` memories load fully into the system prompt; everything else gets
a one-line index entry and is fetched on demand via the `recall_memory`
tool. Fifty memories cost fifty lines, not fifty files.

Chat-only, like skills and MCP: bench never loads memory — cross-task
memory contaminates SWE-bench scores (see design doc §4).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rockycode.engine.tools import Tool, _truncate

MEMORY_DIR = Path(".rockycode") / "memory"
INDEX_FILE = "MEMORY.md"

TYPE_DIRS = {
    "fact": "facts", "skill": "skills", "episode": "episodes",
    "feedback": "feedback", "weakness": "weaknesses",
}
ARCHIVE_DIR = "archive"

MAX_INDEX_CHARS = 8_000        # MEMORY.md cap in the system prompt
MAX_FEEDBACK_CHARS = 1_000     # per feedback memory in the system prompt
MAX_DESCRIPTION_CHARS = 150

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_LIST_FIELDS = {"evidence", "triggers"}


@dataclass
class Memory:
    name: str
    type: str = "fact"             # fact | skill | episode | feedback | weakness
    description: str = ""          # one-liner shown in the index
    importance: int = 5            # 1–10
    status: str = "active"         # active | archived
    origin: str = "user"           # user | agent | dream
    created: str = ""              # YYYY-MM-DD
    evidence: list[str] = field(default_factory=list)   # trajectory session ids
    triggers: list[str] = field(default_factory=list)   # globs/keywords (used from M1)
    body: str = ""
    path: Optional[Path] = None


def _slugify(text: str, max_len: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "memory"


def _parse_list(value: str) -> list[str]:
    inner = value.strip().strip("[]")
    return [v.strip().strip("'\"") for v in inner.split(",") if v.strip().strip("'\"")]


def parse_memory(text: str, path: Optional[Path] = None) -> Memory:
    """Lenient parse; a file with no frontmatter is still a valid memory."""
    m = _FRONTMATTER.match(text)
    fields: dict[str, str] = {}
    body = text
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith((" ", "\t", "#")):
                key, _, value = line.partition(":")
                fields[key.strip().lower()] = value.strip()
        body = text[m.end():]
    body = body.strip()

    def clean(key: str, default: str = "") -> str:
        return fields.get(key, default).strip("'\"")

    try:
        importance = max(1, min(10, int(clean("importance", "5"))))
    except ValueError:
        importance = 5

    first_line = body.splitlines()[0].strip() if body else ""
    return Memory(
        name=clean("name") or (path.stem if path else _slugify(first_line)),
        type=clean("type") if clean("type") in TYPE_DIRS else "fact",
        description=(clean("description") or first_line)[:MAX_DESCRIPTION_CHARS],
        importance=importance,
        status=clean("status") or "active",
        origin=clean("origin") or "user",
        created=clean("created"),
        evidence=_parse_list(fields.get("evidence", "")),
        triggers=_parse_list(fields.get("triggers", "")),
        body=body,
        path=path,
    )


def to_markdown(mem: Memory) -> str:
    lines = [
        "---",
        f"name: {mem.name}",
        f"type: {mem.type}",
        f"description: {mem.description}",
        f"importance: {mem.importance}",
        f"status: {mem.status}",
        f"origin: {mem.origin}",
        f"created: {mem.created}",
        f"evidence: [{', '.join(mem.evidence)}]",
        f"triggers: [{', '.join(mem.triggers)}]",
        "---",
        "",
        mem.body.strip(),
        "",
    ]
    return "\n".join(lines)


class MemoryStore:
    """All paths relative to one root: <workdir>/.rockycode/memory."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def for_workdir(cls, workdir: Path) -> "MemoryStore":
        return cls(workdir / MEMORY_DIR)

    def index_text(self) -> str:
        p = self.root / INDEX_FILE
        try:
            return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        except OSError:
            return ""

    def load_all(self, include_archived: bool = False) -> list[Memory]:
        out: list[Memory] = []
        dirs = list(TYPE_DIRS.values()) + ([ARCHIVE_DIR] if include_archived else [])
        for d in dirs:
            folder = self.root / d
            if not folder.is_dir():
                continue
            for f in sorted(folder.glob("*.md")):
                try:
                    mem = parse_memory(f.read_text(encoding="utf-8", errors="replace"), path=f)
                except OSError:
                    continue
                if d == ARCHIVE_DIR:
                    mem.status = "archived"
                out.append(mem)
        return out

    def get(self, name: str) -> Optional[Memory]:
        for mem in self.load_all(include_archived=True):
            if mem.name == name:
                return mem
        return None

    def save(self, mem: Memory) -> Path:
        if not mem.created:
            mem.created = time.strftime("%Y-%m-%d")
        if not mem.name:
            mem.name = _slugify(mem.description or mem.body)
        folder = self.root / TYPE_DIRS.get(mem.type, "facts")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{_slugify(mem.name)}.md"
        if path.exists() and (mem.path is None or path != mem.path):
            path = folder / f"{_slugify(mem.name)}-{time.strftime('%H%M%S')}.md"
        path.write_text(to_markdown(mem), encoding="utf-8")
        mem.path = path
        return path

    def archive(self, name: str) -> bool:
        """Move a memory to archive/ — never delete. Returns False if absent."""
        mem = self.get(name)
        if mem is None or mem.path is None or mem.status == "archived":
            return False
        mem.status = "archived"
        archive = self.root / ARCHIVE_DIR
        archive.mkdir(parents=True, exist_ok=True)
        target = archive / mem.path.name
        if target.exists():
            target = archive / f"{mem.path.stem}-{time.strftime('%H%M%S')}.md"
        target.write_text(to_markdown(mem), encoding="utf-8")
        mem.path.unlink()
        return True

    def search(self, query: str) -> list[Memory]:
        """M0: case-insensitive substring over name/description/body.
        M1 replaces this with hybrid sqlite-vec + FTS search."""
        q = query.lower()
        return [
            m for m in self.load_all()
            if q in m.name.lower() or q in m.description.lower() or q in m.body.lower()
        ]


# ---- system prompt + tools ---------------------------------------------------

def memory_prompt_section(store: MemoryStore) -> str:
    """MEMORY.md + feedback fully; everything else as index lines."""
    parts: list[str] = []

    index = store.index_text().strip()
    if index:
        parts.append(f"# Project memory (MEMORY.md)\n\n{index[:MAX_INDEX_CHARS]}")

    memories = [m for m in store.load_all() if m.status == "active"]
    feedback = [m for m in memories if m.type == "feedback"]
    others = [m for m in memories if m.type != "feedback"]

    if feedback:
        notes = "\n\n".join(f"- {m.body[:MAX_FEEDBACK_CHARS]}" for m in feedback)
        parts.append(f"# User feedback (always follow)\n\n{notes}")

    if others:
        lines = [f"- {m.name} [{m.type}] — {m.description}" for m in others]
        parts.append(
            "# Memories available\n\n"
            "Knowledge from past sessions. When one looks relevant, call the "
            "`recall_memory` tool with its name — or a free-text `query` to "
            "search by meaning — before relying on it.\n\n"
            + "\n".join(lines)
        )

    if not parts:
        return ""
    return "\n\n" + "\n\n".join(parts)


def build_memory_tools(store: MemoryStore, index=None) -> list[Tool]:
    """`index` is a memory.index.MemoryIndex for semantic recall (M1);
    None keeps the M0 exact-name behavior with substring fallback."""
    recall_schema = {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": (
                "Look up memories. Pass `name` for an exact entry from the "
                "'Memories available' list, or `query` to search by meaning "
                "(English or Chinese). Call this before relying on a memory."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact memory name from the index."},
                    "query": {"type": "string", "description": "Free-text search when no exact name is known."},
                },
                "required": [],
            },
        },
    }

    def _render(mem: Memory) -> str:
        note = " (archived — may be outdated or superseded)" if mem.status == "archived" else ""
        return f"# memory: {mem.name} [{mem.type}]{note}\n\n{mem.body}"

    async def recall(name: str = "", query: str = "") -> str:
        if name:
            mem = store.get(name)
            if mem is None:
                available = ", ".join(m.name for m in store.load_all()) or "(none)"
                return f"[error] no memory named '{name}'. available: {available}"
            return _truncate(_render(mem))
        if not query:
            return "[error] pass either name or query"
        hits: list[Memory] = []
        if index is not None:
            try:
                hits = [m for m, _ in await index.search(query, k=3)]
            except Exception:  # noqa: BLE001 — semantic search is best-effort
                hits = []
        if not hits:
            hits = store.search(query)[:3]
        if not hits:
            return f"[error] nothing in memory matches '{query}'"
        return _truncate("\n\n---\n\n".join(_render(m) for m in hits))

    remember_schema = {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Save a memory for future sessions: a project fact, a verified how-to, "
                "or user feedback. Keep it one focused fact per call."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short kebab-case identifier."},
                    "content": {"type": "string", "description": "The memory body (markdown)."},
                    "type": {
                        "type": "string",
                        "enum": sorted(TYPE_DIRS),
                        "description": "fact (project knowledge), skill (verified how-to), "
                        "episode (what happened), feedback (user guidance).",
                    },
                    "description": {"type": "string", "description": "One-line summary for the index."},
                },
                "required": ["name", "content", "type"],
            },
        },
    }

    async def remember(name: str, content: str, type: str, description: str = "") -> str:
        mem = Memory(
            name=name,
            type=type if type in TYPE_DIRS else "fact",
            description=description or content.splitlines()[0][:MAX_DESCRIPTION_CHARS],
            body=content,
            origin="agent",
        )
        path = store.save(mem)
        return f"[ok] remembered '{mem.name}' ({mem.type}) at {path}"

    return [
        Tool(name="recall_memory", schema=recall_schema, fn=recall, risk="safe"),
        Tool(name="remember", schema=remember_schema, fn=remember, risk="moderate"),
    ]
