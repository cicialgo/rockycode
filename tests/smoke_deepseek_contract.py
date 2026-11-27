"""Flash HTTP contract regression: actual SDK, synthetic SSE, no network/key."""
import asyncio
import copy
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
from openai import AsyncOpenAI

home = Path(tempfile.mkdtemp(prefix="rocky-flash-contract-"))
os.environ["ROCKYCODE_HOME"] = str(home)
os.environ["ROCKYCODE_OLLAMA_URL"] = "http://127.0.0.1:9"

from rockycode.engine import compaction, providers as P
from rockycode.engine.events import EngineError, TurnFinished
from rockycode.engine.loop import Engine
from rockycode.engine.tools import Tool
from rockycode.session import load_history

P.MODELS_TOML = home / "models.toml"
P.ENDPOINTS_TOML = home / "endpoints.toml"
P.PROVIDERS_TOML = home / "providers.toml"
catalog = P.catalog()
ds = catalog["deepseek"]
echoed = []


async def echo(value):
    echoed.append(value)
    return value


registry = {"echo": Tool("echo", {"type": "function", "function": {
    "name": "echo", "description": "Echo a marker", "parameters": {
        "type": "object", "properties": {"value": {"type": "string"}},
        "required": ["value"]}}}, echo, risk="safe")}


def sse(reasoning="r", *, tool=False, finish="stop", text="done", tool_name="echo", value="ok"):
    deltas = [{"reasoning_content": reasoning[:1]}, {"reasoning_content": reasoning[1:]}]
    if tool:
        deltas.extend([{"tool_calls": [{"index": 0, "id": "call_echo", "type": "function",
                                        "function": {"name": tool_name, "arguments": '{"value":'}}]},
                       {"tool_calls": [{"index": 0, "function": {"arguments": json.dumps(value) + "}"}}]}])
    else:
        deltas.append({"content": text})
    chunks = [{"choices": [{"index": 0, "delta": d, "finish_reason": None}]} for d in deltas]
    if finish is not None:
        chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                       "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}})
    for chunk in chunks:
        chunk.update(id="fixture", object="chat.completion.chunk", created=1, model="fixture")
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content="".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
                          + "data: [DONE]\n\n")


def engine(client, *, tools=True, thinking=True, max_tokens=4096):
    e = Engine("deepseek-flash", client=client, registry=registry if tools else {},
               thinking=thinking, workdir=home, max_tokens=max_tokens, max_steps=4,
               finalize_steps=0)
    e.adopt(ds, ds.endpoints[0], "deepseek-flash")
    e.evolve_enabled = False
    return e


async def main():
    captured = []
    replies = [sse("first trace", tool=True, finish="tool_calls"), sse("final trace"),
               sse("next trace"), sse("restored trace"), sse("mm trace"), sse("back trace")]

    def respond(request):
        captured.append(json.loads(request.content))
        return replies.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        async with AsyncOpenAI(api_key="test", base_url="https://example.invalid/v1", http_client=http) as client:
            e = engine(client)
            events = [x async for x in e.run_turn("echo ok")]
            assert echoed == ["ok"]
            assert any(isinstance(x, TurnFinished) for x in events)
            assert not any(isinstance(x, EngineError) for x in events)
            assert captured[0]["top_p"] == 1 and "temperature" not in captured[0]
            assert captured[1]["messages"][2]["reasoning_content"] == "first trace"
            assert e.history[-1]["reasoning_content"] == "final trace"
            await collect(e.run_turn("next user request"))
            assert [m["reasoning_content"] for m in captured[2]["messages"] if m["role"] == "assistant"] == [
                "first trace", "final trace"]
            restored = engine(client)
            restored.resume(load_history(e.trajectory.path))
            await collect(restored.run_turn("after restart"))
            assert any(m.get("reasoning_content") == "next trace" for m in captured[3]["messages"])
            mm = catalog["minimax"]
            restored.switch_provider(client, "minimax-m3", provider_name="minimax",
                                     reasoning_policy=mm.reasoning, provider=mm, endpoint=mm.endpoints[0])
            await collect(restored.run_turn("switch model"))
            assert all("reasoning_content" not in m for m in captured[4]["messages"])
            assert "top_p" not in captured[4] and "temperature" not in captured[4]
            assert captured[4]["model"] == "minimax-m3"
            assert captured[4]["reasoning_split"] is True
            restored.switch_provider(client, "deepseek-flash", provider_name="deepseek",
                                     reasoning_policy=ds.reasoning, provider=ds, endpoint=ds.endpoints[0])
            await collect(restored.run_turn("back to flash"))
            assert all("reasoning_content" in m for m in captured[5]["messages"] if m["role"] == "assistant")

            # Summarizer receives a projected copy with the entire protected tail.
            before = copy.deepcopy(restored.history)
            compact_requests = []

            async def summarize_reply(**kwargs):
                compact_requests.append(kwargs)
                return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="state"))], usage=None)

            fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=summarize_reply)))
            await compaction.summarize(fake, restored.model, restored._api_messages(), [registry["echo"].schema],
                                      protocol=restored.protocol)
            assert any(m.get("reasoning_content") == "first trace" for m in compact_requests[0]["messages"])
            assert restored.history == before

    # Every failure must leave complete history and never execute a partial tool.
    for finish in ("length", "content_filter", "insufficient_system_resource", None, "stop"):
        calls_before = len(echoed)
        response = sse("truncated", tool=True, finish=finish)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response)) as http:
            async with AsyncOpenAI(api_key="test", http_client=http) as client:
                e = engine(client)
                events = await collect(e.run_turn("do not run partial tools"))
                assert any(isinstance(x, EngineError) for x in events), finish
                assert not any(isinstance(x, TurnFinished) for x in events), finish
                assert len(echoed) == calls_before
                assert [m["role"] for m in e.history] == ["system", "user"]
    # No-tools history omits reasoning on the wire but retains it durably.
    for thinking in (True, False):
        bodies = []
        def respond_plain(r):
            bodies.append(json.loads(r.content))
            return sse()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond_plain)) as http:
            async with AsyncOpenAI(api_key="test", http_client=http) as client:
                e = engine(client, tools=False, thinking=thinking, max_tokens=512)
                e.resume([{"role": "assistant", "content": "previous", "reasoning_content": "saved"}])
                events = await collect(e.run_turn("plain"))
                assert not any(isinstance(x, EngineError) for x in events)
                assert bodies[0]["max_tokens"] == 512
                assert all("reasoning_content" not in m for m in bodies[0]["messages"])
                assert ("top_p" in bodies[0]) == thinking
                assert ("temperature" in bodies[0]) != thinking
                assert e.history[1]["reasoning_content"] == "saved"

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse(tool=True, finish=None).content.replace(b"data: [DONE]\n\n", b"")
            raise httpx.ReadError("private server detail")

    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=BrokenStream()))) as http:
        async with AsyncOpenAI(api_key="test", http_client=http) as client:
            before = len(echoed)
            e = engine(client)
            events = await collect(e.run_turn("broken stream"))
            assert len(echoed) == before
            errors = [x.message for x in events if isinstance(x, EngineError)]
            assert errors and "private server detail" not in str(errors)
            assert not any(isinstance(x, TurnFinished) for x in events)

    # The local live canary runs the same Engine and real on-disk resume under mock HTTP.
    from rockycode.flash_probe import probe, MARKER
    def canary_reply(r):
        body = json.loads(r.content)
        last = body["messages"][-1]
        tool = last["role"] == "user" and last["content"].startswith("Call probe_echo")
        return sse("canary trace", tool=tool, finish="tool_calls" if tool else "stop",
                   text=MARKER, tool_name="probe_echo", value=MARKER)
    async with httpx.AsyncClient(transport=httpx.MockTransport(canary_reply)) as http:
        async with AsyncOpenAI(api_key="test", http_client=http) as client:
            report = await probe(client, max_tokens=4096, timeout=5)
            assert report["passed"], report
            assert len(report["calls"]) == 5
            assert all(c["with_reasoning"] == c["assistant_messages"] for c in report["calls"])

    # Per-model contract, not all providers with reasoning='thinking'.
    reasoning_models = {"deepseek-flash", "minimax-m3.1-flash", "mimo-v2.6-pro",
                        "kimi-k3.1-pro", "qwen4-max"}
    for provider in catalog.values():
        for model in provider.models:
            p = provider.spec(model).protocol
            assert (p.replay_reasoning != "never") == (model in reasoning_models)
    assert compaction.estimate_tokens([{"role": "assistant", "content": "", "reasoning_content": "a"*12000}]) > 3000

    # Migration pairs actual legacy trace records, never borrows one across a user turn.
    legacy = home / "legacy.jsonl"
    records = [{"kind": "reasoning", "data": {"text": "legacy"}},
               {"kind": "message", "data": {"role": "assistant", "content": "a"}},
               {"kind": "reasoning", "data": {"text": "orphan"}},
               {"kind": "message", "data": {"role": "user", "content": "b"}},
               {"kind": "message", "data": {"role": "assistant", "content": "c"}}]
    legacy.write_text("\n".join(json.dumps(r) for r in records))
    history = load_history(legacy)
    assert history[0]["reasoning_content"] == "legacy" and "reasoning_content" not in history[2]

    # Compaction snapshots replace older messages; subsequent turns remain appended.
    snapshot = [{"role": "system", "content": "system"},
                {"role": "user", "content": "[context compacted] state"},
                {"role": "assistant", "content": "tail", "reasoning_content": "retained"}]
    with legacy.open("a") as f:
        f.write("\n" + json.dumps({"kind": "compaction", "data": {"new_history": snapshot}}))
        f.write("\n" + json.dumps({"kind": "message", "data": {"role": "user", "content": "next"}}))
    assert load_history(legacy) == snapshot + [{"role": "user", "content": "next"}]


async def collect(stream):
    return [x async for x in stream]


asyncio.run(main())
print("DEEPSEEK CONTRACT OK: SDK wire, tools, final reasoning, resume, compaction, model switch, truncation")
