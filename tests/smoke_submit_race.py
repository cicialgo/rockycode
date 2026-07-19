"""Regression: submitting a new message while a tool runs must not BRICK the
session.

The cancelled turn's finally and the next turn both repair orphaned tool_calls.
If repair weren't idempotent + in-position, the two would race: the old finally
appended an interrupt stub at the END of history — after the new turn's freshly
appended user message and its own repair stub — producing a DUPLICATE +
misordered tool message that DeepSeek 400s on forever (unrecoverable).

This drives _repair_history through that exact interleaving and asserts the
history stays API-valid: each assistant tool_calls id has exactly one response,
immediately after it, no duplicates.

Pure in-memory, no API — deterministic, free, every-commit safe.
"""
import tempfile
import types
from pathlib import Path

from rockycode.engine.loop import Engine


def _engine() -> Engine:
    fake = types.SimpleNamespace(chat=types.SimpleNamespace(completions=None))
    return Engine(model="fake", client=fake, workdir=Path(tempfile.mkdtemp(prefix="rockyrace-")))


def assert_valid(history: list[dict]) -> None:
    for i, m in enumerate(history):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [tc["id"] for tc in m["tool_calls"]]
            resp, j = [], i + 1
            while j < len(history) and history[j].get("role") == "tool":
                resp.append(history[j]["tool_call_id"])
                j += 1
            assert sorted(resp) == sorted(ids), f"msg {i}: ids {ids} but responses right after = {resp}"
    tool_ids = [m["tool_call_id"] for m in history if m.get("role") == "tool"]
    assert len(tool_ids) == len(set(tool_ids)), f"duplicate tool responses: {tool_ids}"


def _asst(*ids):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": i, "type": "function", "function": {"name": "t", "arguments": "{}"}} for i in ids]}


# --- the race: new turn's user message already appended before old repair runs --
eng = _engine()
eng.history = [
    {"role": "user", "content": "do it"},
    _asst("a", "b"),
    {"role": "tool", "tool_call_id": "a", "content": "ok"},   # only 'a' answered
    {"role": "user", "content": "actually do this instead"},  # the new submit
]
eng._repair_history()
assert_valid(eng.history)                       # 'b' stub inserted BEFORE the new user msg
assert eng.history[3]["tool_call_id"] == "b", eng.history  # in-position, not at the end

n = len(eng.history)
eng._repair_history()                           # next turn repairs again
assert len(eng.history) == n, "repair not idempotent — it double-inserted a duplicate"
assert_valid(eng.history)

# --- multi-orphan: two separate tool_calls messages, each missing a response ----
eng2 = _engine()
eng2.history = [_asst("x"), {"role": "user", "content": "mid"}, _asst("y")]
eng2._repair_history()
assert_valid(eng2.history)

print("SUBMIT-RACE SMOKE OK — repair is in-position, idempotent, multi-orphan. amaze!")
