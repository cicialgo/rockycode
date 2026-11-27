"""DeepSeek aliases: actual Engine wire requests + bounded live-probe fake HTTP."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from rockycode import model_probe as probe
from rockycode.engine import providers as P
from rockycode.engine.events import EngineError
from rockycode.engine.loop import Engine
from rockycode.engine.trajectory import TrajectoryLogger
from rockycode.pricing import UsageLedger, default_pricing


def reply(body, *, finish="stop"):
    tools = body.get("tools")
    first_tool = tools and not any(m["role"] == "tool" for m in body["messages"])
    delta = ({"tool_calls": [{"index": 0, "id": "call_probe", "type": "function", "function": {
        "name": "probe_echo", "arguments": '{"value":"probe_ok"}'}}]} if first_tool
        else {"content": "probe_ok" if tools else "437"})
    delta["reasoning_content"] = "fixture trace"
    chunks = [
        {"id": "probe", "object": "chat.completion.chunk", "created": 0, "model": "fixture-model",
         "choices": [{"index": 0, "delta": delta,
                      "finish_reason": "tool_calls" if first_tool else finish}]},
        {"id": "probe", "object": "chat.completion.chunk", "created": 0, "model": "fixture-model",
         "choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 5,
                                   "total_tokens": 35, "prompt_cache_hit_tokens": 10}},
    ]
    data = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=data)


async def main():
    provider, data = probe.bundled_provider()
    catalog = P._providers_from_data(data)
    captured = []

    def handle(request):
        assert str(request.url) == probe.BASE_URL + "/chat/completions"
        body = json.loads(request.content)
        captured.append(body)
        return reply(body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        async with AsyncOpenAI(api_key="test-key", base_url=probe.BASE_URL, http_client=http) as client:
            with tempfile.TemporaryDirectory() as tmp, \
                 patch.object(P, "discover", return_value=catalog), \
                 patch("openai.AsyncOpenAI", return_value=client), \
                 patch.dict(os.environ, {"ROCKYCODE_API_KEY": "test-key"}):
                # No injected client: exercise the real launch resolver as CLI/desktop do.
                for thinking, effort in ((False, "high"), (True, "low"), (True, "high"), (True, "max")):
                    requests = []
                    for requested in (*probe.IDS, *("deepseek:" + m for m in probe.IDS)):
                        engine = Engine(requested, registry={}, workdir=Path(tmp), thinking=thinking,
                                        reasoning_effort=effort, max_tokens=1024,
                                        trajectory_logger=TrajectoryLogger(meta={}, enabled=False))
                        events = [event async for event in engine.run_turn("Compute 19 * 23.")]
                        assert not any(isinstance(event, EngineError) for event in events)
                        assert engine.model == engine.wire_model == "deepseek-flash"
                        assert engine.vision_enabled and engine.context_window == 1048576
                        requests.append(captured[-1])
                    assert all(request == requests[0] for request in requests)
                    expected = probe.offline_report()["models"][0]["effort_bodies"][effort if thinking else "off"]
                    assert requests[0]["thinking"] == expected["thinking"]
                    assert requests[0].get("reasoning_effort") == expected.get("reasoning_effort")
                # /model switches also adopt the same row.
                engine = Engine("deepseek-v4-pro", client=client, registry={}, workdir=Path(tmp),
                                trajectory_logger=TrajectoryLogger(meta={}, enabled=False))
                for requested in probe.IDS:
                    p, ep, model = P.resolve(requested)
                    engine.switch_provider(client, model, provider_name=ep.eid,
                                           reasoning_policy=p.reasoning, provider=p, endpoint=ep)
                    assert engine.model == engine.wire_model == "deepseek-flash" and engine.vision_enabled
            # The probe deliberately bypasses canonical wire naming, but keeps the real loop.
            for model in probe.IDS:
                for case in probe.CASES:
                    start = len(captured)
                    row = await probe.run_case(client, model, case, max_tokens=1024, timeout=5)
                    assert row["passed"], row
                    assert len(row["calls"]) == (2 if case[0] == "tool_high" else 1)
                    assert all(body["model"] == model for body in captured[start:])
                    assert row["calls"][0]["usage"]["prompt_cache_hit_tokens"] == 10
                    assert row["calls"][0]["ttft_ms"] is not None
                    assert row["calls"][0]["response_models"] == ["fixture-model"]
                    if case[0] == "tool_high":
                        assert all(c["assistant_messages"] == c["with_reasoning"] for c in row["calls"])
                        assert any(m.get("reasoning_content") == "fixture trace"
                                   for m in captured[-1]["messages"])
                    assert "test-key" not in json.dumps(row)

    for response, expected in ((httpx.Response(401, json={"error": {"message": "SECRET response"}}), "engine_error"),
                               (reply({"tools": [], "messages": []}, finish="length"), "truncated"),
                               (reply({"tools": [], "messages": []}, finish=None), "incomplete")):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response)) as http:
            async with AsyncOpenAI(api_key="test-key", base_url=probe.BASE_URL,
                                   http_client=http, max_retries=0) as client:
                row = await probe.run_case(client, probe.IDS[0], probe.CASES[0], max_tokens=1024, timeout=5)
                assert not row["passed"]
                if expected == "engine_error":
                    assert expected in row["errors"] and row["calls"][0]["http_status"] == 401
                else:
                    assert not row["complete"]
                    assert row["truncated"] == (expected == "truncated")
                assert "SECRET" not in json.dumps(row)

    with patch.object(P, "registry_data", return_value=data):
        prices = default_pricing()
        for at in (datetime(2026, 10, 21, 2, tzinfo=timezone.utc),
                   datetime(2026, 10, 21, 12, tzinfo=timezone.utc)):
            costs = []
            for model in probe.IDS:
                ledger = UsageLedger(prices)
                ledger.add(model, {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 200,
                                   "completion_tokens": 100}, at=at)
                costs.append((ledger.cost("usd"), ledger.cost("cny")))
            assert costs[0] == costs[1]
    assert probe.offline_report()["equivalent"]
    row = probe.offline_report()["models"][0]
    assert row["max_output"] == 393216 and row["protocol"].startswith("replay_reasoning=with_tools reasoning_required strict_finish")
    assert row["sampling"] == {"on": {"top_p": 1.0}, "off": {"temperature": 1.0}}
    env = {k: v for k, v in os.environ.items() if k != "ROCKYCODE_API_KEY"}
    env.update(OPENAI_API_KEY="unrelated", DEEPSEEK_API_KEY="unrelated")
    missing = subprocess.run([sys.executable, "-m", "rockycode.model_probe", "--live"],
                             env=env, capture_output=True, text=True)
    assert missing.returncode == 2 and "requires ROCKYCODE_API_KEY" in missing.stderr
    print("MODEL PROBE SMOKE OK: alias parity, literal wire IDs, streams, tool round-trip, price, failures")


asyncio.run(main())
