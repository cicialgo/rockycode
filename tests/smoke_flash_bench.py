"""Actual raw-runner SDK payloads, budgets and truncated predictions; no API calls."""
import inspect
import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI
from rich.console import Console

home = Path(tempfile.mkdtemp(prefix="rocky-flash-bench-"))
os.environ["ROCKYCODE_HOME"] = str(home)
os.environ["ROCKYCODE_OLLAMA_URL"] = "http://127.0.0.1:9"

from rockycode.engine import providers as P
from rockycode.runners import raw, agent

P.MODELS_TOML = home / "models.toml"
P.ENDPOINTS_TOML = home / "endpoints.toml"
P.PROVIDERS_TOML = home / "providers.toml"
console = Console(file=io.StringIO(), force_terminal=False)
dataset = [{"instance_id": "synthetic", "repo": "test", "problem_statement": "test"}]

assert inspect.signature(raw.run).parameters["max_tokens"].default == 0
assert inspect.signature(agent.run).parameters["max_tokens"].default == 0
assert inspect.signature(agent.run).parameters["context_window"].default == 0

for model, thinking, cap, finish in (
    ("deepseek-flash", True, 0, "stop"),
    ("deepseek-v4-flash", False, 16384, "stop"),
    ("deepseek-flash", True, 512, "length"),
    ("minimax-m3.1-flash", False, 0, "stop"),
):
    captured = []
    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "fixture", "object": "chat.completion", "created": 1, "model": "fixture",
            "choices": [{"index": 0, "finish_reason": finish, "message": {
                "role": "assistant", "content": "```diff\n--- a/a\n+++ b/a\n```"}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        })
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with OpenAI(api_key="fixture", base_url="https://example.invalid/v1", http_client=http) as client:
            with patch.object(raw, "load_verified", return_value=dataset), \
                 patch("openai.OpenAI", return_value=client), \
                 patch.dict(os.environ, {"ROCKYCODE_API_KEY": "fixture",
                                         "ROCKYCODE_MINIMAX_API_KEY": "fixture"}):
                result = raw.run(model, None, console, thinking=thinking, max_tokens=cap,
                                 output_dir=home / (model + str(cap)))
    request = captured[0]
    settings = json.loads(result.with_suffix(".settings.json").read_text())
    assert settings["max_tokens"] == request["max_tokens"]
    if model.startswith("deepseek"):
        assert request["model"] == "deepseek-flash"
        assert request["max_tokens"] == (cap or 393216)
        assert ("top_p" in request) == thinking
        assert ("temperature" in request) != thinking
        if not thinking:
            assert request["temperature"] == 1.0
    else:
        assert request["model"] == "MiniMax-M3.1-Flash"
        assert request["max_tokens"] == 131072
        assert request["thinking"] == {"type": "adaptive"}
        assert request["reasoning_split"] is True and request["reasoning_effort"] == "low"
        assert "top_p" not in request
    prediction = json.loads(result.read_text())
    assert bool(prediction["model_patch"]) == (finish == "stop")

print("FLASH BENCH OK: model/alias routing, per-model reasoning, budgets, sampling, truncation")
