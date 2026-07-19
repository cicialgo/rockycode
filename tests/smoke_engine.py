"""Headless engine smoke test with a fake DeepSeek stream. No API calls."""
import asyncio
import json
import os
import tempfile
import types
from pathlib import Path

os.chdir(tempfile.mkdtemp(prefix="rockysmoke-"))

from rockycode.engine.loop import Engine
from rockycode.engine.events import (
    TextDelta, ThinkingDelta, ToolFinished, ToolStarted, TurnFinished,
)


class FakeUsage:
    def model_dump(self):
        return {"prompt_tokens": 100, "completion_tokens": 20,
                "prompt_cache_hit_tokens": 64, "prompt_cache_miss_tokens": 36}


def chunk(reasoning=None, content=None, tool_calls=None, usage=None):
    if usage is not None and reasoning is None and content is None and tool_calls is None:
        return types.SimpleNamespace(usage=usage, choices=[])
    delta = types.SimpleNamespace(
        reasoning_content=reasoning, content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(usage=usage, choices=[types.SimpleNamespace(delta=delta)])


def tc(index, id_, name, args):
    return types.SimpleNamespace(
        index=index, id=id_,
        function=types.SimpleNamespace(name=name, arguments=args))


async def stream_from(chunks):
    for c in chunks:
        yield c


class FakeCompletions:
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        assert kwargs["stream"] is True
        for m in kwargs["messages"]:
            assert "reasoning_content" not in m, "reasoning_content leaked into history!"
        if self.calls == 1:
            return stream_from([
                chunk(reasoning="hmm, i check with bash. "),
                chunk(reasoning="question question."),
                chunk(tool_calls=[tc(0, "call_1", "bash", "")]),
                chunk(tool_calls=[tc(0, None, "", '{"command":')]),
                chunk(tool_calls=[tc(0, None, "", ' "echo amaze"}')]),
                chunk(usage=FakeUsage()),
            ])
        return stream_from([
            chunk(reasoning="tool say amaze. good good."),
            chunk(content="echo says **amaze**! we done."),
            chunk(usage=FakeUsage()),
        ])


fake_client = types.SimpleNamespace(
    chat=types.SimpleNamespace(completions=FakeCompletions()))


async def main():
    eng = Engine(model="fake-model", client=fake_client, workdir=Path.cwd())
    events = [ev async for ev in eng.run_turn("does echo work?")]

    kinds = [type(e).__name__ for e in events]
    print("events:", kinds)

    tool_fin = next(e for e in events if isinstance(e, ToolFinished))
    assert tool_fin.ok and "amaze" in tool_fin.output, tool_fin
    assert any(isinstance(e, ThinkingDelta) for e in events)
    reply = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert reply == "echo says **amaze**! we done."
    fin = next(e for e in events if isinstance(e, TurnFinished))
    assert fin.steps == 2 and fin.usage["prompt_cache_hit_tokens"] == 128, fin

    roles = [m["role"] for m in eng.history]
    assert roles == ["system", "user", "assistant", "tool", "assistant"], roles
    assert "tool_calls" in eng.history[2]
    assert all("reasoning_content" not in m for m in eng.history)

    lines = [json.loads(l) for l in eng.trajectory.path.read_text().splitlines()]
    kinds = [l["kind"] for l in lines]
    assert [k for k in kinds if k not in ("usage", "reasoning")] == ["meta"] + ["message"] * 5, kinds
    # per-call usage (incl. cache hit/miss) is now logged — for RL + cache debugging
    assert any(l["kind"] == "usage" and "prompt_cache_hit_tokens" in l["data"] for l in lines), \
        "usage not logged to trajectory"
    # reasoning_content: trajectory-only (history must never carry it — asserted
    # above), one record per API call that actually produced thinking.
    reasoning = [l["data"]["text"] for l in lines if l["kind"] == "reasoning"]
    assert reasoning and all(reasoning), f"reasoning not captured: {kinds}"
    print("trajectory:", eng.trajectory.path)
    print("history roles:", roles)
    print("SMOKE OK — amaze amaze amaze!")


asyncio.run(main())
