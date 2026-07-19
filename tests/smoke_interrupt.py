"""Regression: an interrupted tool batch must NOT leave dangling tool_calls.

Reproduces the prod bug. The model calls two tools; the worker is cancelled
(CancelledError) while the SECOND tool runs — exactly what happens when you
submit a new message mid-web_search. Before the loop.py try/finally fix, history
kept the assistant tool_calls message with only ONE tool response, so the next
request 400'd: "insufficient tool messages following tool_calls". This asserts
the engine now backfills a stub for the unanswered call, in both the live
history and the trajectory that --resume reloads.

Fake DeepSeek stream, no API — deterministic, free, safe for every-commit CI.
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.chdir(tempfile.mkdtemp(prefix="rockyint-"))

from rockycode.engine.loop import Engine
from rockycode.engine.tools import Tool
from invariants import assert_history_api_valid


class FakeUsage:
    def model_dump(self):
        return {"prompt_tokens": 50, "completion_tokens": 10}


def chunk(content=None, tool_calls=None, usage=None):
    delta = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(usage=usage, choices=[types.SimpleNamespace(delta=delta)])


def tc(index, id_, name, args):
    return types.SimpleNamespace(
        index=index, id=id_, function=types.SimpleNamespace(name=name, arguments=args))


async def stream_from(chunks):
    for c in chunks:
        yield c


class FakeCompletions:
    """One assistant turn that asks for two tools; we never reach a 2nd call."""
    async def create(self, **kwargs):
        return stream_from([
            chunk(tool_calls=[tc(0, "call_good", "good_tool", "{}")]),
            chunk(tool_calls=[tc(1, "call_bad", "bad_tool", "{}")]),
            chunk(usage=FakeUsage()),
        ])


_SCHEMA = {"type": "function",
           "function": {"name": "x", "parameters": {"type": "object", "properties": {}}}}


async def main():
    async def good(**kw):
        return "good result"

    async def bad(**kw):
        # Simulate the worker being cancelled exactly while this tool runs.
        # CancelledError is BaseException, so execute() does NOT swallow it —
        # it propagates to the run_turn batch loop, just like a real @work cancel.
        raise asyncio.CancelledError()

    registry = {
        "good_tool": Tool(name="good_tool", schema=_SCHEMA, fn=good),
        "bad_tool": Tool(name="bad_tool", schema=_SCHEMA, fn=bad),
    }
    fake_client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=FakeCompletions()))
    eng = Engine(model="fake", client=fake_client, workdir=Path.cwd(), registry=registry)

    interrupted = False
    try:
        async for _ in eng.run_turn("call two tools"):
            pass
    except asyncio.CancelledError:
        interrupted = True

    assert interrupted, "expected the simulated cancellation to propagate out of run_turn"

    roles = [m["role"] for m in eng.history]
    print("history roles:", roles)

    # Core guard: both tool_calls answered → history is valid for the next request.
    assert_history_api_valid(eng.history)

    # Be specific: good_tool kept its real output; bad_tool got the interrupt stub.
    tool_msgs = {m["tool_call_id"]: m["content"] for m in eng.history if m["role"] == "tool"}
    assert tool_msgs.get("call_good") == "good result", tool_msgs
    assert "interrupted" in tool_msgs.get("call_bad", ""), tool_msgs

    # And the trajectory that --resume reloads is just as valid.
    from rockycode.session import load_history
    assert_history_api_valid(load_history(eng.trajectory.path))

    print("INTERRUPT SMOKE OK — no dangling tool_calls after cancel. amaze!")


asyncio.run(main())
