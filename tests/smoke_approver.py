"""Loop-level test for the injected approver seam (engine/loop.py).

Asserts: (1) the default engine runs tools with no approver (opt-in: today's
behavior unchanged); (2) an injected approver that denies a tool blocks it —
the tool fn never runs, the model gets a '[denied]' result so it can adapt, the
approver is actually consulted, and history stays API-valid (every tool_call
answered). Fake DeepSeek stream, no API.
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.chdir(tempfile.mkdtemp(prefix="rockyapprove-"))

from rockycode.engine.loop import Engine
from rockycode.engine.tools import Tool
from invariants import assert_history_api_valid


class FakeUsage:
    def model_dump(self):
        return {"prompt_tokens": 40, "completion_tokens": 8}


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
    """Turn 1: model calls bash. Turn 2: model gives a final answer."""
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return stream_from([
                chunk(tool_calls=[tc(0, "call_b", "bash", '{"command":"echo hi"}')]),
                chunk(usage=FakeUsage()),
            ])
        return stream_from([chunk(content="all done."), chunk(usage=FakeUsage())])


_SCHEMA = {"type": "function",
           "function": {"name": "bash", "parameters": {"type": "object", "properties": {}}}}


def make_engine(approver=None):
    ran = {"bash": False}

    async def bash_fn(command):
        ran["bash"] = True
        return f"ran: {command}"

    registry = {"bash": Tool(name="bash", schema=_SCHEMA, fn=bash_fn, risk="risky")}
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions()))
    eng = Engine(model="fake", client=client, workdir=Path.cwd(),
                 registry=registry, approver=approver)
    return eng, ran


def tool_result(eng):
    return next(m["content"] for m in eng.history if m["role"] == "tool")


async def main():
    # (1) No approver → tool runs (opt-in default unchanged).
    eng, ran = make_engine(approver=None)
    async for _ in eng.run_turn("run bash"):
        pass
    assert ran["bash"] is True, "default engine must run tools"
    assert tool_result(eng) == "ran: echo hi", eng.history
    assert_history_api_valid(eng.history)

    # (2) Deny approver → tool is blocked, model gets a denial, history valid.
    seen = []

    async def deny_bash(name, args):
        seen.append((name, args))
        return name != "bash"  # allow everything except bash

    eng2, ran2 = make_engine(approver=deny_bash)
    async for _ in eng2.run_turn("run bash"):
        pass
    assert ran2["bash"] is False, "denied tool must NOT execute"
    assert tool_result(eng2).startswith("[denied]"), tool_result(eng2)
    assert seen and seen[0][0] == "bash", f"approver not consulted: {seen}"
    assert seen[0][1] == {"command": "echo hi"}, "approver should get parsed args"
    assert_history_api_valid(eng2.history)  # the [denied] result answers the tool_call
    roles = [m["role"] for m in eng2.history]
    assert roles == ["system", "user", "assistant", "tool", "assistant"], roles

    print("APPROVER SMOKE OK — gate honored, denial fed back, history valid. amaze!")


asyncio.run(main())
