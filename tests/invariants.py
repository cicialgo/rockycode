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
from types import SimpleNamespace


def completed_chunks(chunks):
    """Finish a successful synthetic response with a real terminal choice.

    Legacy tool fixtures used bare EOF. Keep malformed-stream regressions
    separate: this helper is only for scripted successful responses.
    """
    tools = False
    for chunk in chunks:
        for choice in chunk.choices:
            assert getattr(choice, "finish_reason", None) is None
            tools = tools or bool(getattr(choice.delta, "tool_calls", None))
        yield chunk
    yield SimpleNamespace(usage=None, choices=[SimpleNamespace(
        delta=SimpleNamespace(content=None, reasoning_content=None, tool_calls=None),
        finish_reason="tool_calls" if tools else "stop")])


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


# ---- import layering ----------------------------------------------------------

# What rockycode.engine.* may import from the package besides itself. These are
# foundation modules: no UI, no feature logic, no model loop of their own.
ENGINE_MAY_IMPORT = (
    "rockycode",            # the package itself (version)
    "rockycode.engine",
    "rockycode.palette",
    "rockycode.banner",
    "rockycode.config",
    "rockycode.onboarding",
    "rockycode.pricing",
    "rockycode.prompts",
    "rockycode.session",
)


def assert_engine_imports_downward(package_root) -> int:
    """Raise AssertionError if any module under rockycode/engine imports a
    rockycode module outside ENGINE_MAY_IMPORT. Returns the module count."""
    import ast
    from pathlib import Path

    root = Path(package_root)
    bad: list[str] = []
    n = 0
    for path in sorted((root / "engine").rglob("*.py")):
        n += 1
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            for name in names:
                if not name.startswith("rockycode"):
                    continue
                if any(name == ok or name.startswith(ok + ".") for ok in ENGINE_MAY_IMPORT):
                    continue
                bad.append(f"{path.relative_to(root.parent)}:{node.lineno} imports {name}")
    assert not bad, "engine imports upward:\n  " + "\n  ".join(bad)
    return n
