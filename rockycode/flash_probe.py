"""Bounded Flash/MiniMax contract canary. No network without --live; no user tools/files.

This tests the real Engine, including a persisted-history restart. It is NOT a
quality benchmark or an old-vs-new weights comparison. Reads only the explicit
Rocky-owned key for the selected model, never .env or project configuration.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import tomllib
from pathlib import Path
from types import SimpleNamespace

from openai import AsyncOpenAI

from rockycode.engine import providers as P
from rockycode.engine.events import EngineError, TextDelta, TurnFinished
from rockycode.engine.loop import Engine
from rockycode.engine.tools import Tool
from rockycode.engine.trajectory import TrajectoryLogger
from rockycode.session import load_history

MARKER = "flash_probe_ok"
BASE_URL = "https://api.deepseek.com/v1"
TARGETS = {
    "deepseek-flash": ("deepseek", BASE_URL, "ROCKYCODE_API_KEY"),
    "minimax-m3.1-flash": ("minimax", "https://api.minimax.cn/v1", "ROCKYCODE_MINIMAX_API_KEY"),
}


class Recorder:
    def __init__(self, client):
        self.client = client
        self.chat = SimpleNamespace(completions=self)
        self.calls = []

    async def create(self, **kwargs):
        assistants = [m for m in kwargs["messages"] if m["role"] == "assistant"]
        self.calls.append({
            "model": kwargs["model"], "max_tokens": kwargs["max_tokens"],
            "top_p": kwargs.get("top_p"), "temperature": kwargs.get("temperature"),
            "extra_body": kwargs.get("extra_body"), "assistant_messages": len(assistants),
            "with_reasoning": sum("reasoning_content" in m for m in assistants),
        })
        return await self.client.chat.completions.create(**kwargs)


async def probe(client, *, model="deepseek-flash", effort="high", max_tokens=4096, timeout=180):
    data = tomllib.loads(P.BUILTIN_MODELS_TOML.read_text(encoding="utf-8"))
    provider = P._providers_from_data(data)[TARGETS[model][0]]
    recorder = Recorder(client)
    echoed = []

    async def echo(value: str):
        if value != MARKER:
            raise ValueError("unexpected probe marker")
        echoed.append(value)
        return value

    registry = {"probe_echo": Tool("probe_echo", {"type": "function", "function": {
        "name": "probe_echo", "description": "Echo the test marker", "parameters": {
            "type": "object", "properties": {"value": {"type": "string"}},
            "required": ["value"], "additionalProperties": False}}}, echo, risk="safe")}
    results = []
    with tempfile.TemporaryDirectory(prefix="rocky-flash-probe-") as tmp:
        def make_engine():
            engine = Engine(model, client=recorder, workdir=Path(tmp), registry=registry,
                            thinking=True, reasoning_effort=effort, max_tokens=max_tokens,
                            max_steps=2, finalize_steps=0,
                            system_prompt="Follow the test instructions exactly. Use only probe_echo.",
                            trajectory_logger=TrajectoryLogger({}, directory=Path(tmp)))
            engine.adopt(provider, provider.endpoints[0], model)
            return engine

        engine = make_engine()
        tool_prompt = f'Call probe_echo once with value="{MARKER}", then reply exactly with its result.'
        for name, prompt, expected_tools in (
            ("tool_round_trip", tool_prompt, 1),
            ("next_user_turn", f"Reply exactly {MARKER} without calling tools.", 0),
            ("restart_and_tool", tool_prompt, 1),
        ):
            if name == "restart_and_tool":
                history = load_history(engine.trajectory.path)
                engine = make_engine()
                engine.resume(history)
            answer, errors, completed = [], [], False
            before, start = len(echoed), len(recorder.calls)
            try:
                async with asyncio.timeout(timeout):
                    async for event in engine.run_turn(prompt):
                        if isinstance(event, TextDelta):
                            answer.append(event.text)
                        elif isinstance(event, EngineError):
                            errors.append("engine_error")  # no server bodies/credentials in report
                        elif isinstance(event, TurnFinished):
                            completed = True
            except Exception as exc:
                errors.append(type(exc).__name__)
            actual_tools = len(echoed) - before
            results.append({"case": name, "completed": completed, "errors": errors,
                            "requests": len(recorder.calls) - start, "tools": actual_tools,
                            "passed": completed and not errors and actual_tools == expected_tools
                            and "".join(answer).strip() == MARKER})
    return {"kind": "contract_canary_not_quality_benchmark", "results": results,
            "calls": recorder.calls, "passed": all(r["passed"] for r in results)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model", choices=tuple(TARGETS), default="deepseek-flash")
    parser.add_argument("--effort", choices=("low", "high", "max"), default="high")
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    _, base_url, key_env = TARGETS[args.model]
    if not 512 <= args.max_tokens <= 32768 or not 1 <= args.timeout <= 600:
        parser.error("max-tokens must be 512..32768; timeout must be 1..600 seconds per case")
    if not args.live:
        print(json.dumps({"live": False, "model": args.model, "base_url": base_url,
                          "max_requests": 6, "max_output_per_request": args.max_tokens,
                          "next": f"Pass --live with {key_env} set to run the bounded canary."}))
        return 0
    key = (os.environ.get(key_env) or "").strip()
    if key.lower() in P._PLACEHOLDERS:
        parser.error(f"--live requires {key_env} in the environment")

    async def run():
        import httpx
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as http:
            async with AsyncOpenAI(api_key=key, base_url=base_url, max_retries=0,
                                   timeout=args.timeout, http_client=http) as client:
                return await probe(client, model=args.model, effort=args.effort,
                                   max_tokens=args.max_tokens, timeout=args.timeout)
    result = asyncio.run(run())
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
