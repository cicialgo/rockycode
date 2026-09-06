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
    TextDelta, ThinkingDelta, ToolFinished, TurnFinished,
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


# ── cache observability: eviction caught from API numbers, own resets quiet ──
_client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=None))
eng2 = Engine(model="fake", client=_client, workdir=Path.cwd())
# request 1 seeds the baseline — never an event
assert eng2._observe_cache({"prompt_tokens": 10_000, "prompt_cache_hit_tokens": 0}) is None
# healthy follow-up: hit ≈ previous prompt (64-block floor) → quiet
assert eng2._observe_cache({"prompt_tokens": 10_100, "prompt_cache_hit_tokens": 9_984}) is None
# surprise drop → CacheReset with the API's numbers
from rockycode.engine.events import CacheReset
ev = eng2._observe_cache({"prompt_tokens": 10_200, "prompt_cache_hit_tokens": 128})
assert isinstance(ev, CacheReset) and ev.expected_tokens == (10_100 // 64) * 64, ev
assert ev.hit_tokens == 128
# a reset rocky caused (model switch / compaction / mode) mutes the detector once
eng2._mark_cache_reset("model switch")
assert eng2._observe_cache({"prompt_tokens": 10_300, "prompt_cache_hit_tokens": 0}) is None
ev = eng2._observe_cache({"prompt_tokens": 10_400, "prompt_cache_hit_tokens": 0})
assert isinstance(ev, CacheReset), "mute lasts exactly one request"
# a provider that reports no cache fields is never observed (kimi/glm today)
assert eng2._observe_cache({"prompt_tokens": 99_999}) is None
# tiny sessions never alarm (2048-token floor)
eng3 = Engine(model="fake", client=_client, workdir=Path.cwd())
assert eng3._observe_cache({"prompt_tokens": 500, "prompt_cache_hit_tokens": 0}) is None
assert eng3._observe_cache({"prompt_tokens": 600, "prompt_cache_hit_tokens": 0}) is None
# trajectory carries the notes for offline analysis (reason + idle gap)
notes = [json.loads(l) for l in eng2.trajectory.path.read_text().splitlines()]
cache_notes = [n["data"]["cache"] for n in notes
               if n.get("kind") == "note" and "cache" in n.get("data", {})]
reasons = [c["reason"] for c in cache_notes]
assert "evicted" in reasons and "model switch" in reasons, reasons
assert all("idle_s" in c and "hit" in c and "expected" in c for c in cache_notes)
print("cache watch: eviction event from API numbers · own resets logged quiet  ✓")

# ── free-window date refresh: only at reset moments, only on a calendar flip ──
stale = "You are rocky.\n\nToday is 2020-01-01 (Wednesday)."
eng4 = Engine(model="fake", client=_client, workdir=Path.cwd(), system_prompt=stale)
eng4._mark_cache_reset("compaction")
head = eng4.history[0]["content"]
assert "2020-01-01" not in head and "Today is" in head, "stale date re-stamped"
assert eng4._base_system == head, "base + live prompt stay in lockstep"
before = eng4.history[0]["content"]
eng4._mark_cache_reset("compaction")  # same day again → byte-identical no-op
assert eng4.history[0]["content"] == before
eng5 = Engine(model="fake", client=_client, workdir=Path.cwd(),
              system_prompt="bench prompt — no date line")
eng5._mark_cache_reset("model switch")
assert eng5.history[0]["content"] == "bench prompt — no date line", \
    "prompts without with_today (bench) are never touched"
print("prefix refresh: date re-stamped in the free window; bench prompts untouched  ✓")
