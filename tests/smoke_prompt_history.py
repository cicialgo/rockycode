"""PromptHistory: the persistent up-arrow list — JSONL round-trip, adjacent
dedupe, LIMIT cap, corruption tolerance, ChatInput seeding, and no pasted key
kept (masked on the way in, an older file cleaned on load, the file 0600). No tty."""
import json
import os
import stat
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

# a pasted key is not kept: masked before it reaches the list or the disk
SHAPED = "sk-" + "Zx9q" * 8                 # built here: never a key-shaped literal in the repo
OPAQUE = "plain-opaque-key-value-0001"      # no known shape — known only as a key variable's value
os.environ["ROCKYCODE_KIMI_API_KEY"] = OPAQUE
os.environ["OPENAI_BASE_URL"] = "https://gateway.example/v1"  # a wide-list name: an ordinary word here
try:
    p4 = d / "keys.jsonl"
    h5 = PromptHistory(p4)
    h5.append(f"/key {SHAPED}")
    h5.append(f"why does {OPAQUE} get a 401?")
    h5.append("call https://gateway.example/v1 with curl")
    assert h5.items == ["/key [redacted: api key]", "why does [redacted] get a 401?",
                        "call https://gateway.example/v1 with curl"], h5.items
    assert SHAPED not in p4.read_text() and OPAQUE not in p4.read_text()
    assert stat.S_IMODE(p4.stat().st_mode) == 0o600, oct(p4.stat().st_mode)
    print("keys: a pasted key is masked in memory and on disk; ordinary text is left alone; the file is 0600  ✓")

    # a file from before: world-readable, with a key in it → cleaned and closed on load
    p5 = d / "old.jsonl"
    p5.write_text(json.dumps({"input": "hello"}) + "\n" + json.dumps({"input": f"/model {SHAPED}"}) + "\n")
    p5.chmod(0o644)
    assert PromptHistory(p5).items == ["hello", "/model [redacted: api key]"]
    assert SHAPED not in p5.read_text() and stat.S_IMODE(p5.stat().st_mode) == 0o600
    untouched = p.stat().st_mtime_ns
    PromptHistory(p)
    assert p.stat().st_mtime_ns == untouched, "a clean file is not rewritten"
    print("keys: an older file is cleaned and made private the next time it loads  ✓")
finally:
    os.environ.pop("ROCKYCODE_KIMI_API_KEY")
    os.environ.pop("OPENAI_BASE_URL")

print("PROMPT HISTORY SMOKE OK — up-arrow survives restarts, keys do not. amaze!")
