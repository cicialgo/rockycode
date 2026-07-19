"""Compaction smoke test with fake DeepSeek streams. No API calls.

Covers both stages:
  A. summarize — the API reports a huge prompt; pruning can't help (tiny
     tool outputs), so the engine makes one non-streaming summary call and
     rebuilds history as [system, state, tail].
  B. prune — an old bulky tool output is stubbed in place, no API call.
Plus unit checks on the tail boundary (never strands a tool message).
"""
import asyncio
import json
import os
import tempfile
import types
from pathlib import Path

os.chdir(tempfile.mkdtemp(prefix="rockysmoke-"))

from rockycode.engine import compaction
from rockycode.engine.events import AgentState, Compacted, StateChanged, TurnFinished
from rockycode.engine.loop import Engine


class FakeUsage:
    def __init__(self, **kw):
        self.kw = kw

    def model_dump(self):
        return dict(self.kw)


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


def completion(content, usage):
    msg = types.SimpleNamespace(content=content)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)


def client_of(completions):
    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))


def assert_no_orphan_tools(history):
    for i, m in enumerate(history):
        if m["role"] == "tool":
            prev = history[i - 1]
            assert prev["role"] == "tool" or (
                prev["role"] == "assistant" and prev.get("tool_calls")
            ), f"orphan tool message at index {i}"


# ---- unit: tail boundary ----------------------------------------------------

def test_tail_boundary():
    big = "x" * 6000
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "a", "type": "function",
                         "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": big},
        {"role": "assistant", "content": "done"},
    ]
    # Budget that lands inside the tool group: the tail must grow back to
    # the assistant parent, never start on the tool message.
    start = compaction.tail_start(history, budget_tokens=2100)
    assert history[start]["role"] != "tool", f"tail starts on tool at {start}"
    assert start >= 1
    # A huge budget still compresses the older half (the floor): floor lands
    # on the tool at index 3, then retreats to its assistant parent at 2.
    start = compaction.tail_start(history, budget_tokens=10**9)
    assert start == 2, start
    print("tail boundary OK")


# ---- unit: oversized single message -----------------------------------------

def test_truncate_oversized():
    huge = "P" * 300_000  # a pasted log the recent tail would keep verbatim
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": huge},
        {"role": "assistant", "content": "ok"},
        {"role": "tool", "tool_call_id": "a", "content": "z" * 300_000},
    ]
    n = compaction.truncate_oversized(history)
    assert n == 2, n
    assert history[0]["content"] == "sys"                         # system prompt untouched
    assert history[2]["content"] == "ok"                          # small message untouched
    body = history[1]["content"]
    assert len(body) <= compaction.MAX_MSG_CHARS + 80, len(body)
    assert "elided to fit the context window" in body
    assert body.startswith("P") and body.endswith("P")           # head + tail preserved
    assert compaction.truncate_oversized(history) == 0           # nothing left over the cap
    assert compaction.estimate_tokens(history) < int(131_072 * 0.75)  # now fits the window
    print("truncate oversized OK")


# ---- scenario A: summarize --------------------------------------------------

SUMMARY_TEXT = "1. Task: check echo. 2. State: ran `echo hi`, works. 3. Failures: none. 4. Next: report."


class SummarizeFake:
    def __init__(self):
        self.stream_calls = 0
        self.summary_calls = 0

    async def create(self, **kw):
        if not kw.get("stream"):
            self.summary_calls += 1
            assert kw["extra_body"] == {"thinking": {"type": "disabled"}}
            assert kw["tool_choice"] == "none"
            assert kw["messages"][-1]["role"] == "user"
            assert "state document" in kw["messages"][-1]["content"]
            return completion(SUMMARY_TEXT, FakeUsage(prompt_tokens=5000, completion_tokens=40))
        self.stream_calls += 1
        if self.stream_calls == 1:
            return stream_from([
                chunk(tool_calls=[tc(0, "call_1", "bash", '{"command": "echo hi"}')]),
                chunk(usage=FakeUsage(prompt_tokens=5000, completion_tokens=10)),
            ])
        return stream_from([
            chunk(content="done. amaze!"),
            chunk(usage=FakeUsage(prompt_tokens=200, completion_tokens=5)),
        ])


async def test_summarize_path():
    fake = SummarizeFake()
    eng = Engine(model="fake", client=client_of(fake), workdir=Path.cwd(),
                 context_window=1000)  # limit = 750 < the fake's 5000 prompt tokens
    events = [ev async for ev in eng.run_turn("does echo work?")]

    assert fake.summary_calls == 1
    comp = next(e for e in events if isinstance(e, Compacted))
    assert comp.strategy == "summarize", comp
    assert comp.tokens_before > 750 and comp.tokens_after < 750, comp
    assert any(isinstance(e, StateChanged) and e.state == AgentState.COMPACTING for e in events)

    # History was rebuilt: [system, state, ...tail..., final answer].
    assert eng.history[0]["role"] == "system"
    assert eng.history[1]["role"] == "user"
    assert "[context compacted]" in eng.history[1]["content"]
    assert SUMMARY_TEXT in eng.history[1]["content"]
    assert eng.history[-1] == {"role": "assistant", "content": "done. amaze!"}
    assert_no_orphan_tools(eng.history)

    # The summary call's usage is folded into the turn totals.
    fin = next(e for e in events if isinstance(e, TurnFinished))
    assert fin.usage["prompt_tokens"] == 5000 + 5000 + 200, fin.usage
    assert fin.usage["completion_tokens"] == 10 + 40 + 5, fin.usage

    lines = [json.loads(l) for l in eng.trajectory.path.read_text().splitlines()]
    rec = next(l for l in lines if l["kind"] == "compaction")["data"]
    assert rec["strategy"] == "summarize"
    assert rec["summary"] == SUMMARY_TEXT
    assert rec["new_history"][0]["role"] == "system"
    print("summarize path OK —", [type(e).__name__ for e in events])


# ---- scenario B: prune ------------------------------------------------------

class PruneFake:
    def __init__(self):
        self.stream_calls = 0

    async def create(self, **kw):
        assert kw.get("stream"), "prune path must not make a summary API call"
        self.stream_calls += 1
        if self.stream_calls == 1:
            cmd = json.dumps({"command": "python3 -c \"print('a' * 6000)\""})
            return stream_from([
                chunk(tool_calls=[tc(0, "c1", "bash", cmd)]),
                chunk(usage=FakeUsage(prompt_tokens=300, completion_tokens=10)),
            ])
        if self.stream_calls == 2:
            return stream_from([
                chunk(tool_calls=[tc(0, "c2", "bash", '{"command": "echo two"}')]),
                chunk(usage=FakeUsage(prompt_tokens=5000, completion_tokens=10)),
            ])
        return stream_from([
            chunk(content="fixed. amaze!"),
            chunk(usage=FakeUsage(prompt_tokens=400, completion_tokens=5)),
        ])


async def test_prune_path():
    fake = PruneFake()
    # limit = 4000: round 1 (300 + small estimate) stays under; round 2's
    # reported 5000 trips it, and stubbing the old 6000-char output suffices.
    eng = Engine(model="fake", client=client_of(fake), workdir=Path.cwd(),
                 context_window=4000, compact_threshold=1.0)
    events = [ev async for ev in eng.run_turn("make output, then fix")]

    comp = next(e for e in events if isinstance(e, Compacted))
    assert comp.strategy == "prune", comp
    assert fake.stream_calls == 3

    big_tool = eng.history[3]
    assert big_tool["role"] == "tool"
    assert big_tool["content"].endswith(compaction.PRUNE_STUB), big_tool["content"][-90:]
    assert len(big_tool["content"]) < 400, len(big_tool["content"])
    small_tool = eng.history[5]
    assert small_tool["role"] == "tool" and "two" in small_tool["content"]
    assert_no_orphan_tools(eng.history)

    lines = [json.loads(l) for l in eng.trajectory.path.read_text().splitlines()]
    rec = next(l for l in lines if l["kind"] == "compaction")["data"]
    assert rec["strategy"] == "prune" and rec["pruned_tool_outputs"] == 1, rec
    print("prune path OK —", [type(e).__name__ for e in events])


async def test_two_tier_context():
    """50% = a soft one-time reminder (no compaction); auto-compaction only near
    the ceiling. The old behavior compacted at 50% — this proves it no longer does."""
    from rockycode.engine.events import ContextReminder
    eng = Engine(model="fake", client=client_of(PruneFake()), workdir=Path.cwd(),
                 context_window=1000)  # remind at 500, auto-compact at 900
    eng.history = [{"role": "system", "content": "s"},
                   {"role": "user", "content": "u"},
                   {"role": "assistant", "content": "a"}]

    async def evs_at(tok):
        eng._last_prompt_tokens = tok
        eng._sent_until = len(eng.history)  # projected == tok (nothing appended since)
        return [e async for e in eng._maybe_compact({})]

    def has(evs, t):
        return any(isinstance(e, t) for e in evs)

    assert not has(await evs_at(400), ContextReminder), "reminded under 50%"
    e1 = await evs_at(600)
    assert has(e1, ContextReminder), "no reminder crossing 50%"
    assert not has(e1, StateChanged), "must NOT compact at 60% — only remind"
    assert not has(await evs_at(700), ContextReminder), "reminded twice without re-arm"
    await evs_at(400)                                    # drop under → re-arm
    assert has(await evs_at(600), ContextReminder), "did not re-arm below 50%"
    print("two-tier context: remind @50% (no compact @60%), re-arms  ✓")


async def main():
    test_tail_boundary()
    await test_summarize_path()
    await test_prune_path()
    await test_two_tier_context()
    print("SMOKE OK — context squished, nothing lost. amaze!")


asyncio.run(main())
