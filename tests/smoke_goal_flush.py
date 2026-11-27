"""Goal-mode context flush: Engine.flush_to drops a milestone's history back to
the seed, and EngineDriver calls it at the start of every work turn.

No API calls. Same shape as tests/smoke_compaction.py.
"""
import asyncio
import copy
import json
import os
import tempfile
import types
from pathlib import Path

os.chdir(tempfile.mkdtemp(prefix="rockysmoke-"))

from rockycode.engine.events import Compacted, StateChanged, TextDelta, TurnFinished
from rockycode.engine.loop import Engine
from rockycode.goal.runner import EngineDriver
from rockycode.pricing import UsageLedger
from rockycode.session import load_history


def client_of(completions):
    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))


class _Quiet:
    async def create(self, **kw):
        raise AssertionError(f"flush path must not call the model ({kw.get('stream')})")


def make_engine(**kw):
    kw.setdefault("system_prompt", "sys")
    return Engine(
        model="fake",
        client=client_of(_Quiet()),
        workdir=Path.cwd(),
        registry={},
        **kw,
    )


def traj(eng):
    return [json.loads(line) for line in eng.trajectory.path.read_text().splitlines() if line.strip()]


def test_inplace_flush():
    eng = make_engine()
    eng._append({"role": "user", "content": "u0"})
    seed = copy.deepcopy(eng.history)
    eng._append({"role": "assistant", "content": "looking"})
    eng._append({"role": "user", "content": "go on"})
    eng._append({
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "bash", "arguments": "{}"}}],
    })
    eng._append({"role": "tool", "tool_call_id": "c1", "content": "ok"})
    eng._last_prompt_tokens = 900_000
    eng._sent_until = len(eng.history)
    eng._reminded = True
    dropped = eng.flush_to(seed, reason="goal milestone: in place")
    assert dropped == 4, dropped
    assert eng.history == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u0"},
    ], eng.history
    assert eng._last_prompt_tokens == 0 and eng._sent_until == 0 and eng._reminded is False
    assert eng.stats.compactions == 0, eng.stats.compactions
    last = traj(eng)[-1]
    assert last["kind"] == "compaction", last
    data = last["data"]
    assert data["strategy"] == "flush" and data["cache_kept"] is True, data
    assert data["new_history"] == eng.history, data["new_history"]
    print("in-place flush drops the tail, keeps the prefix, zeros counters  ✓")


def test_system_prompt_kept():
    eng = make_engine()
    eng._append({"role": "user", "content": "u0"})
    seed = copy.deepcopy(eng.history)
    eng.history[0]["content"] = "CURRENT SYSTEM"
    eng._append({"role": "assistant", "content": "tail"})
    dropped = eng.flush_to(seed, reason="keep system")
    assert dropped == 1, dropped
    assert eng.history[0]["content"] == "CURRENT SYSTEM"
    assert eng.history[1:] == [{"role": "user", "content": "u0"}]
    print("flush keeps the current system prompt  ✓")


def test_rewritten_prefix():
    eng = make_engine()
    eng._append({"role": "user", "content": "u0"})
    seed = copy.deepcopy(eng.history)
    eng.history[0]["content"] = "CURRENT SYSTEM"
    eng.history[:] = [
        eng.history[0],
        {"role": "user", "content": "[context compacted] ..."},
        {"role": "assistant", "content": "tail"},
    ]
    dropped = eng.flush_to(seed, reason="rewritten")
    assert dropped == 1, dropped
    assert eng.history == [
        {"role": "system", "content": "CURRENT SYSTEM"},
        {"role": "user", "content": "u0"},
    ], eng.history
    assert eng._cache_reset_reason == "context flush", eng._cache_reset_reason
    data = traj(eng)[-1]["data"]
    assert data["strategy"] == "flush" and data["cache_kept"] is False, data
    print("rewritten prefix is replaced from the seed; cache reset logged  ✓")


def test_noop():
    eng = make_engine()
    eng._append({"role": "user", "content": "u0"})
    seed = copy.deepcopy(eng.history)
    eng._last_prompt_tokens = 42
    eng._sent_until = 1
    eng._reminded = True
    before = len(traj(eng))
    assert eng.flush_to(seed, reason="noop") == 0
    assert len(traj(eng)) == before
    assert eng._last_prompt_tokens == 42 and eng._sent_until == 1 and eng._reminded is True
    print("flush of an unchanged seed is a no-op  ✓")


def test_resume_replay():
    eng = make_engine()
    eng._append({"role": "user", "content": "u0"})
    seed = copy.deepcopy(eng.history)
    eng._append({"role": "assistant", "content": "gone"})
    eng.flush_to(seed, reason="resume")
    eng._append({"role": "user", "content": "new-user"})
    assert load_history(eng.trajectory.path) == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u0"},
        {"role": "user", "content": "new-user"},
    ]
    print("resume replays the flush snapshot, then messages after it  ✓")


async def test_no_spurious_compaction():
    eng = make_engine(system_prompt="s", context_window=1000)
    eng._append({"role": "user", "content": "u0"})
    eng._append({"role": "assistant", "content": "a0"})
    seed = copy.deepcopy(eng.history)
    eng._append({"role": "user", "content": "tail that must not stick"})
    eng._last_prompt_tokens = 950
    eng._sent_until = len(eng.history)
    assert eng.flush_to(seed, reason="goal milestone: no compact") == 1
    evs = [e async for e in eng._maybe_compact({})]
    assert not any(isinstance(e, (Compacted, StateChanged)) for e in evs), evs
    print("flushed counters do not trip a compaction  ✓")


class FakeEngine:
    def __init__(self):
        self.history = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "seed"},
        ]
        self.model = "m"
        self.flush_calls = []

    def flush_to(self, seed, reason=""):
        self.flush_calls.append((seed, reason))
        return 0

    async def run_turn(self, prompt):
        yield TextDelta("ok")
        yield TurnFinished(steps=1, usage={})


async def test_driver_wiring():
    attached = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "seed"},
    ]
    fake = FakeEngine()
    drv = EngineDriver(client=None, model="m", reviewer_model="m",
                       workspace=None, ledger=UsageLedger())
    drv.attach(fake)
    fake.history[0]["content"] = "mutated after attach"
    fake.history.append({"role": "assistant", "content": "later"})
    await drv.work("alpha milestone", "ctx")
    await drv.work("beta", "ctx")
    assert len(fake.flush_calls) == 2, fake.flush_calls
    for seed, reason in fake.flush_calls:
        assert seed == attached, seed
        assert reason.startswith("goal milestone:"), reason
    assert fake.flush_calls[0][1] == "goal milestone: alpha milestone"

    off = FakeEngine()
    quiet = EngineDriver(client=None, model="m", reviewer_model="m",
                         workspace=None, ledger=UsageLedger(), flush_context=False)
    quiet.attach(off)
    await quiet.work("alpha", "ctx")
    assert off.flush_calls == [], off.flush_calls
    print("driver flushes to the attach-time seed; flush_context=False stays quiet  ✓")


async def main():
    test_inplace_flush()
    test_system_prompt_kept()
    test_rewritten_prefix()
    test_noop()
    test_resume_replay()
    await test_no_spurious_compaction()
    await test_driver_wiring()
    print("GOAL FLUSH SMOKE OK — fork-and-flush per milestone. amaze!")


asyncio.run(main())
