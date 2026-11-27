"""Bounded DeepSeek alias diagnostic: python -m rockycode.model_probe [--live].

Default: bundled catalog only, no credentials, user overrides or network.
--live: synthetic prompts through the real Engine, forcing each literal wire ID
at the transport boundary (ordinary Rocky resolves both to deepseek-flash).
No shell/file/MCP tools, history, memory or persistent trajectories are loaded.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from rockycode.engine import providers as P
from rockycode.engine.effort import build_extra_body

IDS = ("deepseek-flash", "deepseek-v4-flash")
BASE_URL = "https://api.deepseek.com/v1"
CASES = (
    ("text_off", False, "Compute 19 * 23. Reply with only the integer."),
    ("text_high", True, "Compute 19 * 23. Reply with only the integer."),
    ("tool_high", True, 'Call probe_echo with value="probe_ok" once, then repeat its result.'),
)


def bundled_provider():
    data = tomllib.loads(P.BUILTIN_MODELS_TOML.read_text(encoding="utf-8"))
    return P._providers_from_data(data)["deepseek"], data


def offline_report() -> dict:
    provider, data = bundled_provider()
    rows = []
    for requested in IDS:
        model = provider.aliases.get(requested, requested)
        spec = provider.spec(model)
        rows.append({
            "requested": requested, "resolved": model, "wire": provider.api_model(model),
            "context": spec.context, "max_output": spec.max_output, "vision": spec.vision,
            "protocol": spec.protocol.label,
            "sampling": {"on": spec.protocol.sampling(True),
                         "off": spec.protocol.sampling(False)},
            "price": spec.price,
            "effort_bodies": {dial: build_extra_body(
                dial != "off", dial if dial != "off" else "high", provider.reasoning,
                efforts=provider.efforts_for(model), thinking_off=provider.thinking_off_for(model),
            ) for dial in ("off", "low", "high", "max")},
        })
    comparable = [{k: v for k, v in row.items() if k != "requested"} for row in rows]
    metadata = data["models"]["deepseek-flash"]
    return {"mode": "offline", "catalog_scope": "bundled; user overrides excluded",
            "equivalent": comparable[0] == comparable[1], "models": rows,
            "documented_served_model": metadata.get("served_model"),
            "verified_at": metadata.get("verified_at"), "sources": metadata.get("sources", [])}


class RecordingTransport:
    """Observe the Engine's actual stream while preserving the literal test ID."""

    def __init__(self, client, wire_id: str):
        self.client, self.wire_id = client, wire_id
        self.chat = SimpleNamespace(completions=self)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        kwargs = {**kwargs, "model": self.wire_id}
        record = {"request_model": self.wire_id, "max_tokens": kwargs["max_tokens"],
                  "top_p": kwargs.get("top_p"), "temperature": kwargs.get("temperature"),
                  "assistant_messages": sum(m["role"] == "assistant" for m in kwargs["messages"]),
                  "with_reasoning": sum(m["role"] == "assistant" and "reasoning_content" in m
                                        for m in kwargs["messages"]),
                  "extra_body": copy.deepcopy(kwargs["extra_body"]), "response_models": [],
                  "finish_reasons": [], "usage": {}, "ttft_ms": None, "latency_ms": None}
        self.calls.append(record)
        start = time.monotonic()
        try:
            stream = await self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            record["http_status"] = getattr(exc, "status_code", None)
            record["latency_ms"] = round((time.monotonic() - start) * 1000, 2)
            raise

        async def observe():
            try:
                async for chunk in stream:
                    model = getattr(chunk, "model", None)
                    if model and model not in record["response_models"]:
                        record["response_models"].append(model)
                    if chunk.usage is not None:
                        usage = chunk.usage.model_dump()
                        # Allowlist numbers only: never persist response bodies or headers.
                        record["usage"] = {k: usage[k] for k in (
                            "prompt_tokens", "completion_tokens", "total_tokens",
                            "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
                        ) if isinstance(usage.get(k), int)}
                    for choice in chunk.choices:
                        delta = choice.delta
                        if record["ttft_ms"] is None and any(getattr(delta, k, None) for k in (
                                "content", "reasoning_content", "tool_calls")):
                            record["ttft_ms"] = round((time.monotonic() - start) * 1000, 2)
                        reason = getattr(choice, "finish_reason", None)
                        if reason:
                            record["finish_reasons"].append(reason)
                    yield chunk
            finally:
                record["latency_ms"] = round((time.monotonic() - start) * 1000, 2)
                await stream.close()
        return observe()


async def run_case(client, wire_id: str, case: tuple, *, max_tokens: int, timeout: float) -> dict:
    from rockycode.engine.events import EngineError, TextDelta, ToolFinished
    from rockycode.engine.loop import Engine
    from rockycode.engine.tools import Tool
    from rockycode.engine.trajectory import TrajectoryLogger

    name, thinking, prompt = case
    transport = RecordingTransport(client, wire_id)
    provider, _ = bundled_provider()
    echo_values = []

    async def echo(value: str) -> str:
        if value != "probe_ok":
            raise ValueError("unexpected synthetic probe argument")
        echo_values.append(value)
        return value

    registry = {}
    if name == "tool_high":
        registry["probe_echo"] = Tool("probe_echo", {
            "type": "function", "function": {"name": "probe_echo", "description": "Echo a probe marker.",
                "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                               "required": ["value"], "additionalProperties": False}},
        }, echo, risk="safe")
    answer, errors, finished_tools = [], [], 0
    with TemporaryDirectory(prefix="rocky-model-probe-") as tmp:
        engine = Engine("deepseek-flash", client=transport, registry=registry, workdir=Path(tmp),
                        thinking=thinking, reasoning_effort="high", max_tokens=max_tokens,
                        max_steps=2 if registry else 1, finalize_steps=0,
                        system_prompt="You are a concise test assistant. Follow the synthetic task exactly.",
                        trajectory_logger=TrajectoryLogger(meta={}, enabled=False))
        engine.adopt(provider, provider.endpoints[0], "deepseek-flash")
        try:
            async with asyncio.timeout(timeout):
                async for event in engine.run_turn(prompt):
                    if isinstance(event, TextDelta):
                        answer.append(event.text)
                    elif isinstance(event, EngineError):
                        errors.append("engine_error")  # messages can contain provider/credential data
                    elif isinstance(event, ToolFinished):
                        finished_tools += 1
        except Exception as exc:  # failure on one ID must not hide the other ID's result
            errors.append(type(exc).__name__)
    text = "".join(answer).strip()
    truncated = any("length" in call["finish_reasons"] for call in transport.calls)
    complete = bool(transport.calls) and all(call["finish_reasons"] for call in transport.calls) and (
        transport.calls[-1]["finish_reasons"][-1] == "stop")
    # The tool case checks the round trip (one echo call, one finished tool, the
    # echoed marker repeated in the answer); the exact wording around the marker
    # is model prose, not part of the contract.
    passed = (echo_values == ["probe_ok"] and finished_tools == 1 and "probe_ok" in text
              if registry else text == "437")
    return {"requested": wire_id, "case": name, "passed": passed and complete and not errors and not truncated,
            "errors": errors, "truncated": truncated, "complete": complete, "tool_completions": finished_tools,
            "answer_chars": len(text), "answer_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "calls": transport.calls}


async def live_report(key: str, *, repeats: int, max_tokens: int, timeout: float) -> dict:
    import httpx
    from openai import AsyncOpenAI

    rows = []
    # Fixed official destination. Do not inherit a project URL, HTTP proxy or SDK key.
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as http:
        async with AsyncOpenAI(api_key=key, base_url=BASE_URL, max_retries=0,
                               timeout=timeout, http_client=http) as client:
            for repeat in range(repeats):
                for index, case in enumerate(CASES):
                    # Counterbalance order to reduce (not eliminate) warm-cache/load bias.
                    order = IDS if (repeat + index) % 2 == 0 else tuple(reversed(IDS))
                    for wire_id in order:
                        row = await run_case(client, wire_id, case, max_tokens=max_tokens, timeout=timeout)
                        rows.append({"repeat": repeat + 1, **row})
    return {"mode": "live", "base_url": BASE_URL, "timestamp": datetime.now(timezone.utc).isoformat(),
            "literal_wire_ids": True, "rows": rows, "passed": all(row["passed"] for row in rows),
            "limitations": "Synthetic compatibility probe, not a quality or latency benchmark; "
                           "response model strings do not prove identical weights. No vision/compaction coverage."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="spend API tokens on synthetic official-endpoint calls")
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--max-tokens", type=int, choices=range(1024, 4097), default=4096, metavar="1024..4096")
    parser.add_argument("--timeout", type=float, default=60, help="wall-clock seconds per case (1..120)")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 120:
        parser.error("--timeout must be between 1 and 120")
    report = offline_report()
    if args.live:
        key = (os.environ.get("ROCKYCODE_API_KEY") or "").strip()
        if not key or key.lower() in P._PLACEHOLDERS:
            parser.error("--live requires ROCKYCODE_API_KEY in this shell; no files or other key names are read")
        report["live"] = asyncio.run(live_report(key, repeats=args.repeats,
                                                max_tokens=args.max_tokens, timeout=args.timeout))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["equivalent"] and report.get("live", {}).get("passed", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
