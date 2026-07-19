"""Read-only tool batches run CONCURRENTLY; any batch with a write stays SERIAL.
Responses always come back in the original tool_calls order.

Deterministic: each fake tool bumps a shared 'live' counter on entry and yields,
so a parallel batch reaches live==N while a serial one never exceeds 1 — no
timing assertions. Fake DeepSeek stream, no API.
"""
import asyncio
import types
from pathlib import Path

from rockycode.engine.loop import Engine
from rockycode.engine.tools import Tool

_SCHEMA = {"type": "function", "function": {"name": "x", "parameters": {"type": "object", "properties": {}}}}


class FakeUsage:
    def model_dump(self):
        return {"prompt_tokens": 10, "completion_tokens": 1}


def _chunk(content=None, tool_calls=None, usage=None):
    delta = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(usage=usage, choices=[types.SimpleNamespace(delta=delta)])


def _tc(index, id_, name):
    return types.SimpleNamespace(index=index, id=id_,
                                 function=types.SimpleNamespace(name=name, arguments="{}"))


async def _stream(chunks):
    for c in chunks:
        yield c


class _Client:
    """Emit one tool-call batch, then a final answer."""
    def __init__(self, batch):
        self.batch = batch
        self.n = 0

    async def create(self, **kw):
        self.n += 1
        if self.n == 1:
            return _stream([
                _chunk(tool_calls=[_tc(i, f"c{i}", name) for i, name in enumerate(self.batch)]),
                _chunk(usage=FakeUsage()),
            ])
        return _stream([_chunk(content="done"), _chunk(usage=FakeUsage())])


async def _run(batch, risks):
    live, peak = [0], [0]

    def _tool(name, risk):
        async def fn(**kw):
            live[0] += 1
            peak[0] = max(peak[0], live[0])
            await asyncio.sleep(0.02)  # give siblings a chance to overlap
            live[0] -= 1
            return f"{name}-result"
        return Tool(name=name, schema=_SCHEMA, fn=fn, risk=risk)

    reg = {name: _tool(name, risk) for name, risk in risks.items()}
    eng = Engine(model="fake", registry=reg, workdir=Path("/tmp"),
                 client=types.SimpleNamespace(chat=types.SimpleNamespace(completions=_Client(batch))))
    async for _ in eng.run_turn("go"):
        pass
    tool_msgs = [m for m in eng.history if m["role"] == "tool"]
    return peak[0], tool_msgs


async def main():
    # 3 reads → concurrent (peak == 3), responses in tool_calls order
    peak, msgs = await _run(["read_file", "grep", "glob"],
                            {"read_file": "safe", "grep": "safe", "glob": "safe"})
    assert peak == 3, f"reads did not run concurrently: peak={peak}"
    assert [m["tool_call_id"] for m in msgs] == ["c0", "c1", "c2"], msgs
    assert [m["content"] for m in msgs] == ["read_file-result", "grep-result", "glob-result"], msgs
    print("3 reads ran concurrently (peak=3), responses in order  ✓")

    # a batch containing a write → serial (peak == 1), order preserved
    peak, msgs = await _run(["read_file", "write_file"],
                            {"read_file": "safe", "write_file": "moderate"})
    assert peak == 1, f"a batch with a write must stay serial: peak={peak}"
    assert [m["tool_call_id"] for m in msgs] == ["c0", "c1"], msgs
    print("read+write batch stayed serial (peak=1), order preserved  ✓")

    # single read → serial path (no gather for a lone call)
    peak, _ = await _run(["read_file"], {"read_file": "safe"})
    assert peak == 1
    print("single-call batch uses the serial path  ✓")

    # regression: the SANDBOX registry (goal mode + `chat --sandbox`) must tag
    # reads "safe" too, or _is_read is always False there and reads never
    # parallelize. Docker-free — we only inspect the risk tiers.
    from unittest.mock import MagicMock

    from rockycode.engine.sandbox import build_sandbox_registry
    sreg = build_sandbox_registry(MagicMock())
    assert all(sreg[n].risk == "safe" for n in ("read_file", "grep", "glob")), \
        {n: sreg[n].risk for n in ("read_file", "grep", "glob")}
    assert sreg["bash"].risk == "risky" and sreg["write_file"].risk == "moderate"
    print("sandbox registry carries read=safe / write=moderate / bash=risky  ✓")

    print("PARALLEL SMOKE OK — reads concurrent, writes serial, order preserved. amaze!")


asyncio.run(main())
