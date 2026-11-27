"""Actual MiMo compaction and restart through SDK MockTransport; no API calls."""
import asyncio
import copy
import json
import os
import tempfile
from pathlib import Path

import httpx
from openai import AsyncOpenAI

home = Path(tempfile.mkdtemp(prefix="rocky-mimo-compact-"))
os.environ["ROCKYCODE_HOME"] = str(home)
os.environ["ROCKYCODE_OLLAMA_URL"] = "http://127.0.0.1:9"

from rockycode.engine import compaction, providers as P
from rockycode.engine.events import Compacted, EngineError
from rockycode.engine.loop import Engine
from rockycode.engine.tools import Tool
from rockycode.session import load_history


async def never_run(**kwargs):
    raise AssertionError("compaction/resume must not execute historical or summary tools")


registry = {"read": Tool("read", {"type": "function", "function": {
    "name": "read", "parameters": {"type": "object", "properties": {}}}}, never_run, risk="safe")}


def call(cid):
    return {"id": cid, "type": "function", "function": {"name": "read", "arguments": "{}"}}


def history(system):
    return [system, {"role": "user", "content": "old task"},
            {"role": "assistant", "content": None, "reasoning_content": "old trace", "tool_calls": [call("old")]},
            {"role": "tool", "tool_call_id": "old", "content": "O" * 9000},
            {"role": "assistant", "content": "old done"},
            {"role": "user", "content": "recent task"},
            {"role": "assistant", "content": None, "reasoning_content": "tail trace", "tool_calls": [call("a"), call("b")]},
            {"role": "tool", "tool_call_id": "a", "content": "A"},
            {"role": "tool", "tool_call_id": "b", "content": "B"},
            {"role": "assistant", "content": "recent done", "reasoning_content": "final trace"}]


def engine(client):
    p, ep, model = P.resolve("mimo:mimo-v2.6-pro")
    e = Engine(model, client=client, registry=registry, workdir=home,
               system_prompt="fixture", context_window=2000, compact_threshold=1,
               max_tokens=128, max_steps=1, finalize_steps=0)
    e.adopt(p, ep, model)
    e.evolve_enabled = False
    return e


def assert_groups(messages):
    for i, m in enumerate(messages):
        if m.get("tool_calls"):
            ids = [t["id"] for t in m["tool_calls"]]
            results = messages[i + 1:i + 1 + len(ids)]
            assert [r.get("tool_call_id") for r in results] == ids
            assert all(r["role"] == "tool" for r in results)
            assert m.get("reasoning_content")
    assert any(m.get("reasoning_content") == "tail trace" for m in messages)


async def scenario(finish="stop", *, tool=False, content="state", prune=False):
    captured = []

    def respond(request):
        body = json.loads(request.content)
        captured.append(body)
        assert body["model"] == "mimo-v2.6-pro"
        assert "max_tokens" not in body
        assert_groups(body["messages"])
        if body["stream"]:
            assert body["max_completion_tokens"] == 128
            chunks = [{"id": "fixture", "object": "chat.completion.chunk", "created": 0,
                       "model": body["model"], "choices": [{"index": 0,
                       "delta": {"content": "continued"}, "finish_reason": "stop"}]}]
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n")
        assert not prune, "pruning must not spend a summary call"
        assert "tools" not in body and "tool_choice" not in body
        assert body["thinking"] == {"type": "disabled"}
        assert body["max_completion_tokens"] == compaction.SUMMARY_MAX_TOKENS
        return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 0,
            "model": body["model"], "choices": [{"index": 0, "finish_reason": finish,
                "message": {"role": "assistant", "content": content,
                            "tool_calls": [call("unexpected")] if tool else None}}]})

    async with AsyncOpenAI(api_key="fixture", max_retries=0,
            http_client=httpx.AsyncClient(trust_env=False, transport=httpx.MockTransport(respond))) as client:
        e = engine(client)
        e.resume(history(copy.deepcopy(e.history[0])))
        before = copy.deepcopy(e.history)
        e._last_prompt_tokens = compaction.estimate_tokens(e.history) if prune else 100_000
        e._sent_until = len(e.history)
        events = [x async for x in e._maybe_compact({})]
        success = not prune and finish == "stop" and not tool and bool(content.strip())
        compacted = next(x for x in events if isinstance(x, Compacted))
        assert compacted.strategy == ("summarize" if success else "prune")
        assert any(isinstance(x, EngineError) for x in events) == (not prune and not success)
        assert len(captured) == (0 if prune else 1)
        if success:
            assert e.history[1] == compaction.state_message(content)
        else:
            # Invalid summaries keep the original messages; only old tool output is stubbed.
            before[3]["content"] = e.history[3]["content"]
            assert e.history == before
            assert e.history[3]["content"].endswith(compaction.PRUNE_STUB)
        assert_groups(e.history)
        saved = load_history(e.trajectory.path)
        assert saved == e.history
        restored = engine(client)
        restored.resume(saved)
        events = [x async for x in restored.run_turn("continue after restart")]
        assert not any(isinstance(x, EngineError) for x in events)
        assert len(captured) == (1 if prune else 2)


async def main():
    await scenario()
    await scenario(prune=True)
    for finish, tool, content in [("tool_calls", True, "tempting text"),
                                  ("stop", True, "inconsistent"),
                                  ("length", False, "truncated"),
                                  ("repetition_truncation", False, "repeated"),
                                  (None, False, "unfinished"),
                                  ("stop", False, " ")]:
        await scenario(finish, tool=tool, content=content)


asyncio.run(main())
print("MIMO COMPACTION OK: summary/prune, invalid completion fallback, parallel tail, restart, wire limits")
