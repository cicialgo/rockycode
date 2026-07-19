"""Step-budget smoke test: warnings injected near the cap, hard stop at it."""
import asyncio
import tempfile
import types
from pathlib import Path

from rockycode.engine.events import EngineError, TurnFinished
from rockycode.engine.loop import Engine


def chunk(tool_calls=None, usage=None):
    if usage is not None and tool_calls is None:
        return types.SimpleNamespace(usage=usage, choices=[])
    delta = types.SimpleNamespace(reasoning_content=None, content=None, tool_calls=tool_calls)
    return types.SimpleNamespace(usage=None, choices=[types.SimpleNamespace(delta=delta)])


class ExplorerForever:
    """A model that never stops exploring — bash every step."""

    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        tc = types.SimpleNamespace(
            index=0, id=f"c{self.calls}",
            function=types.SimpleNamespace(name="bash", arguments='{"command": "echo explore"}'))

        async def stream():
            yield chunk(tool_calls=[tc])
        return stream()


async def main():
    fake = types.SimpleNamespace(chat=types.SimpleNamespace(completions=ExplorerForever()))
    eng = Engine(
        model="fake", client=fake, workdir=Path(tempfile.mkdtemp()),
        max_steps=13, finalize_steps=3,
    )
    events = [ev async for ev in eng.run_turn("explore forever")]

    errors = [e for e in events if isinstance(e, EngineError)]
    # hard stop after explore budget + finalize window
    assert errors and "13+3 finalize" in errors[0].message, errors
    assert any(isinstance(e, TurnFinished) for e in events)

    harness_msgs = [m["content"] for m in eng.history
                    if m["role"] == "user" and str(m.get("content", "")).startswith("[harness]")]
    # one soft 10-step warning + one finalize notice
    assert any("10 steps remain" in m for m in harness_msgs), harness_msgs
    assert any("EXPLORE BUDGET SPENT" in m for m in harness_msgs), harness_msgs

    # the explorer never stops calling tools, so it runs the full finalize
    # window: total model calls == max_steps + finalize_steps == 16
    assert fake.chat.completions.calls == 16, fake.chat.completions.calls

    # finalize notice lands right after the explore budget (after 13 tool rounds)
    idx = next(i for i, m in enumerate(eng.history)
               if m["role"] == "user" and "EXPLORE BUDGET SPENT" in str(m.get("content")))
    tools_before = sum(1 for m in eng.history[:idx] if m["role"] == "tool")
    assert tools_before == 13, tools_before

    print(f"soft warning + finalize notice injected; {fake.chat.completions.calls} calls (13+3) ok")
    print("BUDGET SMOKE OK — amaze amaze amaze!")


asyncio.run(main())
