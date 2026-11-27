"""Provider-independent completion gate and per-model history on the real SDK."""
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

home = Path(tempfile.mkdtemp(prefix="rocky-model-protocol-"))
os.environ["ROCKYCODE_HOME"] = str(home)
os.environ["ROCKYCODE_OLLAMA_URL"] = "http://127.0.0.1:9"

from rockycode.engine import providers as P
from rockycode.engine.events import EngineError, TurnFinished
from rockycode.engine.loop import Engine
from rockycode.engine.model_protocol import GENERIC
from rockycode.engine.tools import Tool
from rockycode.session import load_history

executed = []


async def echo(value):
    executed.append(value)
    return value


registry = {"echo": Tool("echo", {"type": "function", "function": {
    "name": "echo", "parameters": {"type": "object", "properties": {
        "value": {"type": "string"}}, "required": ["value"]}}}, echo, risk="safe")}


def reply(*, tool=False, finish="stop", reasoning=None, tool_name="echo", value="ok", text="done", parallel=False):
    deltas = [] if reasoning is None else [{"reasoning_content": reasoning}]
    if tool:
        deltas += [{"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                    "function": {"name": tool_name, "arguments": '{"value":'}}]},
                   {"tool_calls": [{"index": 0, "function": {"arguments": json.dumps(value) + "}"}}]}]
        if parallel:
            deltas.append({"tool_calls": [{"index": 1, "id": "call_2", "type": "function",
                "function": {"name": tool_name, "arguments": json.dumps({"value": "second"})}}]})
    else:
        deltas += [{"content": text}]
    chunks = [{"choices": [{"index": 0, "delta": d, "finish_reason": None}]} for d in deltas]
    if finish is not None:
        chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    for c in chunks:
        c.update(id="fixture", object="chat.completion.chunk", created=0, model="fixture")
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
                          + "data: [DONE]\n\n")


def engine(client, model, *, tools=True):
    p, ep, model = P.resolve(model)
    e = Engine(model, client=client, registry=registry if tools else {},
               workdir=home, max_steps=3, finalize_steps=0, max_tokens=4096)
    e.adopt(p, ep, model)
    e.evolve_enabled = False
    return e


async def turn(e):
    return [x async for x in e.run_turn("fixture")]


async def main():
    # Valid JSON and even [DONE] cannot authorize an unfinished tool batch.
    # Exercise generic and explicit model contracts.
    for model in ("minimax-m3", "kimi-k3", "kimi-k3.1-pro", "deepseek-flash", "minimax-m3.1-flash", "mimo-v2.6-pro"):
        for finish in (None, "stop", "length", "content_filter", "repetition_truncation"):
            before = len(executed)
            async with AsyncOpenAI(api_key="test", max_retries=0,
                    http_client=httpx.AsyncClient(trust_env=False, transport=httpx.MockTransport(
                        lambda r: reply(tool=True, finish=finish)))) as client:
                e = engine(client, model)
                events = await turn(e)
                assert any(isinstance(x, EngineError) for x in events), (model, finish)
                assert not any(isinstance(x, TurnFinished) for x in events)
                assert len(executed) == before
                assert [m["role"] for m in e.history] == ["system", "user"]
    # A terminal tool marker without tools is malformed too. Text-only legacy
    # servers may still omit the marker; they can never execute a tool that way.
    generic = GENERIC
    assert generic.completion_error("tool_calls", tool_calls=False)
    assert generic.completion_error(None, tool_calls=False) is None

    # Kimi K3.1 Pro: replay tool reasoning and preserve final reasoning on resume.
    kimi_requests = []
    kimi_responses = [reply(tool=True, finish="tool_calls", reasoning="tool trace"),
                      reply(reasoning="answer trace"), reply()]

    def kimi_respond(request):
        kimi_requests.append(json.loads(request.content))
        return kimi_responses.pop(0)

    async with AsyncOpenAI(api_key="test", max_retries=0,
            http_client=httpx.AsyncClient(trust_env=False, transport=httpx.MockTransport(kimi_respond))) as client:
        e = engine(client, "kimi-k3.1-pro")
        assert not any(isinstance(x, EngineError) for x in await turn(e))
        assert executed == ["ok"]
        restored = engine(client, "kimi-k3.1-pro", tools=False)
        restored.resume(load_history(e.trajectory.path))
        assert not any(isinstance(x, EngineError) for x in await turn(restored))
        assert executed == ["ok"]
        assert [m["reasoning_content"] for m in kimi_requests[1]["messages"]
                if m["role"] == "assistant"] == ["tool trace"]
        assert [m["reasoning_content"] for m in kimi_requests[2]["messages"]
                if m["role"] == "assistant"] == ["tool trace", "answer trace"]
        for body in kimi_requests:
            assert body["model"] == "kimi-k3.1-pro"
            assert body["max_completion_tokens"] == 4096 and "max_tokens" not in body
            assert "temperature" not in body and "top_p" not in body
        executed.clear()

    # Qwen4: the engine uses the plan key and replays reasoning with and without tools.
    qwen_model = "qwen4-max"
    qwen_route = f"qwen-plan:{qwen_model}"
    old_qwen_route = "qwen-plan:qwen4-max-preview"
    for saved in (old_qwen_route, "qwen4-max-preview"):
        _, endpoint, model = P.resolve(saved)
        assert (endpoint.eid, model) == ("qwen-plan", qwen_model)
    assert P.resolve(qwen_model)[1].eid == "qwen-plan"
    assert P.resolve(f"qwen:{qwen_model}") is None
    assert P.resolve("qwen-plan")[2] == "qwen3.8-max"
    assert P.resolve("qwen-plan:qwen4")[2] == qwen_model
    assert [c.id for c in P.choices() if c.model == qwen_model] == [qwen_route]
    with patch.dict(os.environ, {"ROCKYCODE_API_KEY": "fixture-home", "ROCKYCODE_QWEN_API_KEY": "fixture-paygo"}):
        os.environ.pop("ROCKYCODE_QWEN_PLAN_API_KEY", None)
        assert not any(c.model == qwen_model for c in P.configured_choices())
        try:
            Engine(old_qwen_route, workdir=home, registry={})
        except RuntimeError as error:
            assert "ROCKYCODE_QWEN_PLAN_API_KEY" in str(error)
        else:
            raise AssertionError("Qwen4 must not fall back to the home or pay-as-you-go key")

    qwen_requests = []
    qwen_responses = [reply(tool=True, finish="tool_calls", reasoning="tool trace"),
                      reply(reasoning="answer trace"), reply(), reply(), reply(), reply()]
    plan_url = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"

    def qwen_respond(request):
        assert str(request.url) == plan_url + "/chat/completions"
        assert request.headers["Authorization"] == "Bearer fixture-plan"
        qwen_requests.append(json.loads(request.content))
        return qwen_responses.pop(0)

    with patch.dict(os.environ, {"ROCKYCODE_QWEN_PLAN_API_KEY": "fixture-plan"}):
        assert [c.id for c in P.configured_choices() if c.model == qwen_model] == [qwen_route]
        http_client = httpx.AsyncClient(trust_env=False, transport=httpx.MockTransport(qwen_respond))
        with patch("openai.AsyncOpenAI",
                   side_effect=lambda **kw: AsyncOpenAI(http_client=http_client, **kw)):
            e = Engine(old_qwen_route, workdir=home, registry=registry, max_tokens=4096,
                       max_steps=3, finalize_steps=0, reasoning_effort="high")
        async with e.client:
            assert e.vision_enabled and e.context_window == 1_000_000 and e.model_max_output == 131_072
            e.evolve_enabled = False
            assert any(isinstance(x, TurnFinished) for x in await turn(e))
            assert executed == ["ok"]
            restored = engine(e.client, "qwen4-max-preview", tools=False)
            restored.resume(load_history(e.trajectory.path))
            restored.reasoning_effort = "max"
            assert any(isinstance(x, TurnFinished) for x in await turn(restored))
            restored.thinking = False
            assert any(isinstance(x, TurnFinished) for x in await turn(restored))
            assert executed == ["ok"]
            executed.clear()
            # A saved preview budget must fit the stable API with thinking on and off.
            capped = engine(e.client, qwen_route, tools=False)
            capped.max_tokens = 393_216
            capped.reasoning_effort = "low"
            for thinking in (True, False):
                capped.thinking = thinking
                assert any(isinstance(x, TurnFinished) for x in await turn(capped))
    assert [m["reasoning_content"] for m in qwen_requests[1]["messages"]
            if m["role"] == "assistant"] == ["tool trace"]
    assert [m["reasoning_content"] for m in qwen_requests[2]["messages"]
            if m["role"] == "assistant"] == ["tool trace", "answer trace"]
    assert [b.get("reasoning_effort") for b in qwen_requests] == ["high", "high", "xhigh", None, "low", None]
    assert [b["enable_thinking"] for b in qwen_requests] == [True, True, True, False, True, False]
    for body in qwen_requests:
        assert body["model"] == qwen_model
        assert "max_tokens" not in body
    assert [b["max_completion_tokens"] for b in qwen_requests] == [4096] * 4 + [131_072] * 2
    assert not qwen_responses

    # MiMo: tool traces survive the next user turn, restart and projection.
    captured_mimo = []
    mimo_responses = [reply(tool=True, parallel=True, finish="tool_calls", reasoning="mimo tool trace"),
                      reply(reasoning="mimo final trace"), reply(), reply(), reply(), reply()]

    def mimo_respond(request):
        body = json.loads(request.content)
        captured_mimo.append(body)
        return mimo_responses.pop(0)

    async with AsyncOpenAI(api_key="test", max_retries=0,
            http_client=httpx.AsyncClient(trust_env=False, transport=httpx.MockTransport(mimo_respond))) as client:
        e = engine(client, "mimo-v2.6-pro")
        before = len(executed)
        assert not any(isinstance(x, EngineError) for x in await turn(e))
        assert len(executed) == before + 2
        assert any(m.get("reasoning_content") == "mimo tool trace" for m in captured_mimo[1]["messages"])
        assert [m["tool_call_id"] for m in captured_mimo[1]["messages"] if m["role"] == "tool"] == ["call_1", "call_2"]
        e.thinking = False
        await turn(e)
        assert captured_mimo[2]["thinking"] == {"type": "disabled"}
        assert any(m.get("reasoning_content") == "mimo tool trace" for m in captured_mimo[2]["messages"])
        restored = engine(client, "mimo-v2.6-pro", tools=False)
        restored.resume(load_history(e.trajectory.path))
        await turn(restored)
        assert captured_mimo[3]["thinking"] == {"type": "enabled"}
        assert len(executed) == before + 2  # restoring never replays completed tools
        assert any(m.get("reasoning_content") == "mimo tool trace" for m in captured_mimo[3]["messages"])
        snapshot = copy.deepcopy(restored.history)
        restored.trajectory.compaction({"new_history": snapshot})
        assert load_history(restored.trajectory.path) == snapshot
        mimo = P.resolve("mimo-v2.6-pro")[0].spec("mimo-v2.6-pro").protocol
        assert GENERIC.messages(snapshot, tools=True) != snapshot
        assert mimo.messages(snapshot, tools=True) == snapshot
        assert restored.history == snapshot
        # Legacy missing reasoning remains missing; never invent an empty trace.
        legacy = [{"role": "assistant", "tool_calls": [{"id": "old"}]}]
        assert mimo.messages(legacy, tools=True) == legacy
        assert all(b["model"] == "mimo-v2.6-pro" and "max_tokens" not in b and
                   b["max_completion_tokens"] == 4096 for b in captured_mimo)
        for model in ("minimax-m3", "mimo-v2.6-pro"):
            p, ep, model = P.resolve(model)
            restored.switch_provider(client, model, provider_name=p.name, reasoning_policy=p.reasoning,
                                     provider=p, endpoint=ep)
            await turn(restored)
        assert all("reasoning_content" not in m for m in captured_mimo[4]["messages"])
        assert any(m.get("reasoning_content") == "mimo tool trace" for m in captured_mimo[5]["messages"])
        assert "max_completion_tokens" not in captured_mimo[4] and "max_tokens" in captured_mimo[4]
        assert len(executed) == before + 2
        assert not mimo_responses
        executed.clear()

    captured = []
    responses = [reply(tool=True, finish="tool_calls", reasoning="first trace"),
                 reply(reasoning="final trace"), reply(reasoning="next trace"),
                 reply(tool=True, finish="tool_calls"), reply(), reply(), reply()]

    def respond(request):
        captured.append(json.loads(request.content))
        return responses.pop(0)

    async with AsyncOpenAI(api_key="test", max_retries=0,
            http_client=httpx.AsyncClient(trust_env=False, transport=httpx.MockTransport(respond))) as client:
        e = engine(client, "minimax-m3.1-flash")
        events = await turn(e)
        assert not any(isinstance(x, EngineError) for x in events)
        assert executed == ["ok"]
        body = captured[1]
        assert body["model"] == "MiniMax-M3.1-Flash"
        assert body["thinking"] == {"type": "adaptive"} and body["reasoning_split"] is True
        assert "temperature" not in body and "top_p" not in body
        assert [m["reasoning_content"] for m in body["messages"] if m["role"] == "assistant"] == ["first trace"]
        # A restarted session and a compaction snapshot retain the same trace.
        restored = engine(client, "minimax-m3.1-flash", tools=False)
        restored.resume(load_history(e.trajectory.path))
        await turn(restored)
        assert [m["reasoning_content"] for m in captured[2]["messages"] if m["role"] == "assistant"] == [
            "first trace", "final trace"]
        snapshot = copy.deepcopy(restored.history)
        restored.trajectory.compaction({"new_history": snapshot})
        assert load_history(restored.trajectory.path) == snapshot
        # Simple calls may omit reasoning; do not fabricate an empty trace.
        e = engine(client, "minimax-m3.1-flash")
        await turn(e)
        assert executed == ["ok", "ok"]
        assert all("reasoning_content" not in m for m in captured[4]["messages"])
        # Switching away projects reasoning out without destroying durable data.
        generic_provider, ep, model = P.resolve("minimax-m3")
        restored.switch_provider(client, model, provider_name=generic_provider.name,
                                 reasoning_policy=generic_provider.reasoning,
                                 provider=generic_provider, endpoint=ep)
        await turn(restored)
        assert all("reasoning_content" not in m for m in captured[5]["messages"])
        p, ep, model = P.resolve("minimax-m3.1-flash")
        restored.switch_provider(client, model, provider_name=p.name, reasoning_policy=p.reasoning,
                                 provider=p, endpoint=ep)
        await turn(restored)
        assert any(m.get("reasoning_content") == "first trace" for m in captured[6]["messages"])
        assert not responses

    from rockycode.flash_probe import MARKER, probe

    def canary_reply(request):
        body = json.loads(request.content)
        assert body["model"] == "MiniMax-M3.1-Flash"
        assert body["thinking"] == {"type": "adaptive"}
        last = body["messages"][-1]
        tool = last["role"] == "user" and last["content"].startswith("Call probe_echo")
        return reply(tool=tool, finish="tool_calls" if tool else "stop", reasoning="canary trace",
                     tool_name="probe_echo", value=MARKER, text=MARKER)

    async with AsyncOpenAI(api_key="test", max_retries=0,
            http_client=httpx.AsyncClient(trust_env=False, transport=httpx.MockTransport(canary_reply))) as client:
        report = await probe(client, model="minimax-m3.1-flash", timeout=5)
        assert report["passed"] and len(report["calls"]) == 5, report
        assert all(c["with_reasoning"] == c["assistant_messages"] for c in report["calls"])


asyncio.run(main())
print("MODEL PROTOCOL OK: completion gate, Qwen4 Token Plan, reasoning replay, off/on, resume, switching")
