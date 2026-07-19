"""PromptHistory: the persistent up-arrow list — JSONL round-trip, adjacent
dedupe, LIMIT cap, corruption tolerance, and ChatInput seeding. No tty."""
import json
import tempfile
from pathlib import Path

from rockycode.tui.prompt_history import PromptHistory

d = Path(tempfile.mkdtemp())

# round-trip: appends land in the file and a fresh instance reloads them in order
p = d / "hist.jsonl"
h = PromptHistory(p)
assert h.items == []
h.append("first prompt")
h.append("check tests")
h.append("check tests")          # adjacent duplicate → dropped
h.append("   ")                  # whitespace → dropped
h.append("check tests again")
assert h.items == ["first prompt", "check tests", "check tests again"], h.items
h2 = PromptHistory(p)
assert h2.items == ["first prompt", "check tests", "check tests again"], h2.items
print("round-trip: appended, deduped, reloaded oldest-first  ✓")

# file format: one {"input": ...} JSON object per line (research-branch format)
lines = p.read_text().splitlines()
assert all(json.loads(ln)["input"] for ln in lines) and len(lines) == 3, lines
print("format: append-only JSONL  ✓")

# corruption tolerance: a torn/garbage line is skipped, the rest survives
p2 = d / "torn.jsonl"
p2.write_text('{"input": "good one"}\n{"input": "tor\nnot json at all\n{"input": "good two"}\n')
h3 = PromptHistory(p2)
assert h3.items == ["good one", "good two"], h3.items
print("load: torn lines skipped, no crash  ✓")

# cap: over LIMIT the oldest fall off and the file is rewritten to LIMIT lines
p3 = d / "cap.jsonl"
old_limit = PromptHistory.LIMIT
PromptHistory.LIMIT = 5
try:
    h4 = PromptHistory(p3)
    for i in range(8):
        h4.append(f"prompt {i}")
    assert h4.items == [f"prompt {i}" for i in range(3, 8)], h4.items
    assert len(p3.read_text().splitlines()) == 5, p3.read_text()
    assert PromptHistory(p3).items == h4.items
finally:
    PromptHistory.LIMIT = old_limit
print("cap: LIMIT enforced in memory and on disk  ✓")

# missing file / unreadable path → empty history, never an exception
assert PromptHistory(d / "never-written.jsonl").items == []
print("load: missing file → empty, no crash  ✓")

# ChatInput seeding: a store-backed input starts with the persisted history;
# a bare ChatInput() (every existing test) stays in-memory with no file IO
from rockycode.tui.app import ChatInput  # noqa: E402

inp = ChatInput(history=h2)
assert inp._history == ["first prompt", "check tests", "check tests again"]
assert ChatInput()._history == [] and ChatInput()._store is None
print("ChatInput: seeded from the store; bare construction stays in-memory  ✓")

print("PROMPT HISTORY SMOKE OK — up-arrow survives restarts. amaze!")
