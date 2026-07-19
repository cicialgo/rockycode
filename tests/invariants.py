"""Shared structural invariants for engine history. Import from smoke_*.py.

The big one — `assert_history_api_valid` — encodes the rule that bit us in prod:
every assistant message carrying `tool_calls` must be answered by a `tool`
message for each `tool_call_id`, contiguously, before any later message. If a
tool_call goes unanswered, DeepSeek (OpenAI-compatible) rejects the *next*
request with:

    400 — An assistant message with 'tool_calls' must be followed by tool
    messages responding to each 'tool_call_id'.
    (insufficient tool messages following tool_calls message)

An interrupted turn (new submit / Esc cancels the worker mid-batch), a deny that
forgets to answer, or compaction that splits a tool_calls/tool group all break
this. Because `_append` writes the trajectory immediately, a broken in-memory
history is also a broken `--resume`. Call this anywhere history is produced or
reloaded so the whole class of bug is caught before release, not in the wild.
"""
from __future__ import annotations

from typing import Any


def assert_history_api_valid(history: list[dict[str, Any]]) -> None:
    """Raise AssertionError if any assistant tool_calls message is unanswered."""
    i, n = 0, len(history)
    while i < n:
        msg = history[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            expected = [tc["id"] for tc in msg["tool_calls"]]
            answered: list[str] = []
            j = i + 1
            while j < n and history[j].get("role") == "tool":
                answered.append(history[j].get("tool_call_id"))
                j += 1
            missing = [e for e in expected if e not in answered]
            assert not missing, (
                f"history[{i}] assistant tool_calls {expected} are missing tool "
                f"responses for {missing} (got {answered}). This 400s the next "
                f"request and corrupts --resume."
            )
            i = max(j, i + 1)
        else:
            i += 1
