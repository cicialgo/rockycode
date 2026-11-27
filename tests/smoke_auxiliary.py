"""Short background calls use their model's wire id, thinking policy and budget."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

os.environ["ROCKYCODE_HOME"] = tempfile.mkdtemp(prefix="rocky-auxiliary-")
os.environ["ROCKYCODE_OLLAMA_URL"] = "http://127.0.0.1:9"

from rockycode.dream.core import CloudChat, DreamReport
from rockycode.engine.auxiliary import request_options
from rockycode.engine import route as route_mod
from rockycode.engine.titler import generate_title


async def main():
    captured = []
    finish = "stop"

    def respond(request):
        assert str(request.url) == "https://fixture.invalid/v1/chat/completions"
        body = json.loads(request.content)
        captured.append(body)
        return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 0,
            "model": body["model"], "choices": [{"index": 0, "finish_reason": finish,
                "message": {"role": "assistant", "content": "Fix login tests"}}]})

    async with AsyncOpenAI(api_key="fixture", base_url="https://fixture.invalid/v1", max_retries=0,
            http_client=httpx.AsyncClient(trust_env=False, transport=httpx.MockTransport(respond))) as client:
        for model, wire, thinking, budget in (
            ("deepseek-v4-flash", "deepseek-flash", "disabled", 128),
            ("minimax-m3.1-flash", "MiniMax-M3.1-Flash", "adaptive", 4096),
            ("minimax:MiniMax-M3.1-Flash", "MiniMax-M3.1-Flash", "adaptive", 4096),
            ("minimax-m3", "minimax-m3", "disabled", 128),
        ):
            assert await CloudChat(client, model).chat("fixture", max_tokens=128) == "Fix login tests"
            body = captured[-1]
            assert body["model"] == wire and body["thinking"] == {"type": thinking}
            assert body["max_tokens"] == budget
            if thinking == "adaptive":
                assert body["reasoning_effort"] == "low" and body["reasoning_split"] is True
            if model == "deepseek-v4-flash":
                assert body["temperature"] == 1 and "top_p" not in body
            with patch.object(route_mod, "sidecar_client", return_value=None):
                assert await generate_title(client, "user", "reply", model=model) == "Fix login tests"
            assert captured[-1]["model"] == wire and captured[-1]["max_tokens"] == budget
            assert not client.is_closed()  # the session owns this client
        # Unknown servers get no DeepSeek/MiniMax-only fields.
        assert request_options("unlisted-model", 128) == {"model": "unlisted-model", "max_tokens": 128}
        await CloudChat(client, "unlisted-model").chat("fixture", 128)
        assert "thinking" not in captured[-1] and "reasoning_effort" not in captured[-1]
        # Partial structured output must not become a dream memory/title.
        finish = "length"
        try:
            await CloudChat(client, "minimax-m3.1-flash").chat("fixture")
        except ValueError:
            pass
        else:
            raise AssertionError("truncated dream response accepted")
        with patch.object(route_mod, "sidecar_client", return_value=None):
            assert await generate_title(client, "user", "reply", model="deepseek-flash") is None
        # A separately created sidecar is closed even when generation fails.
        sidecar = AsyncOpenAI(api_key="fixture", base_url="https://fixture.invalid/v1", max_retries=0,
            http_client=httpx.AsyncClient(trust_env=False, transport=httpx.MockTransport(respond)))
        with patch.object(route_mod, "sidecar_client", return_value=(sidecar, "MiniMax-M3.1-Flash")):
            assert await generate_title(client, "user", "reply") is None
        assert sidecar.is_closed() and not client.is_closed()


asyncio.run(main())

# The CLI must select MiniMax's key/URL too, not just fix the request body
# while continuing to send MiniMax ids to the DeepSeek home endpoint.
from typer.testing import CliRunner
from rockycode.cli import app
from rockycode.memory.index import IndexUnavailable

clients = []


class Client:
    def __init__(self, **kwargs):
        self.settings = kwargs
        self.closed = False
        clients.append(self)

    async def close(self):
        self.closed = True


class Runner:
    store = None

    def __init__(self, *args, **kwargs):
        assert kwargs["model"] == "minimax-m3.1-flash"

    async def run(self, **kwargs):
        return DreamReport()


with patch.dict(os.environ, {"ROCKYCODE_MINIMAX_API_KEY": "fixture-minimax",
                             "ROCKYCODE_API_KEY": "fixture-home"}), \
     patch("openai.AsyncOpenAI", Client), patch("rockycode.dream.DreamRunner", Runner), \
     patch("rockycode.memory.index.MemoryIndex", side_effect=IndexUnavailable("offline fixture")):
    result = CliRunner().invoke(app, ["dream", "--model", "minimax-m3.1-flash",
                                    "--workdir", str(Path(os.environ["ROCKYCODE_HOME"]))])
    assert result.exit_code == 0, result.output
    assert clients[-1].settings["base_url"] == "https://api.minimax.cn/v1"
    assert clients[-1].settings["api_key"] == "fixture-minimax"
    assert clients[-1].closed

print("AUXILIARY OK: SDK requests, budgets, truncation, client ownership, CLI endpoint/key routing")
