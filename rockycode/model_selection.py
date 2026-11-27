"""Strict host-selected models and small text calls using existing native contracts.

No endpoint/key arguments from tools, no provider fallback, no new adapter stack.
The Choice is also passed to Engine when injecting its client: injection alone
deliberately bypasses Engine's normal launch resolution.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict
from urllib.parse import urlsplit

from rockycode.engine import providers
from rockycode.engine import route as route_mod
from rockycode.engine.effort import build_extra_body


def select_model(model: str, *, tools: bool = False) -> providers.Choice:
    if not model.strip() or len(model) > 200 or "://" in model:
        raise ValueError("select a registered model ID, not a URL")
    hit = providers.resolve(model)
    if hit is None:
        # A row the registry refused (a misspelt switch) is said, not shrugged at.
        raise ValueError(providers.refused_hint(model)
                         or "unknown or ambiguous model; select a registered endpoint:model")
    choice = providers.Choice(*hit)
    endpoint = urlsplit(choice.endpoint.base_url)
    if endpoint.scheme not in ("http", "https") or not endpoint.hostname:
        raise ValueError("invalid configured model endpoint")
    if choice.provider.reasoning not in providers.REASONING_SHAPES:
        raise ValueError("unsupported model reasoning protocol")
    if tools and choice.provider.tools != "native":
        raise ValueError("selected model does not support the native tools required by this entry point")
    if choice.spec.max_output < 1 or choice.spec.context < 1:
        raise ValueError("invalid model token limits")
    return choice


def model_receipt(choice: providers.Choice) -> dict:
    """Bind review to transport/config, without exposing URL userinfo/path/query or keys."""
    contract = {"provider": choice.provider.name, "endpoint": asdict(choice.endpoint),
                "spec": asdict(choice.spec), "reasoning": choice.provider.reasoning,
                "efforts": choice.provider.efforts_for(choice.model),
                "thinking_off": choice.provider.thinking_off_for(choice.model),
                "tools": choice.provider.tools, "local": choice.provider.local,
                "cache_field": choice.provider.cache_field}
    digest = hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
    url = urlsplit(choice.endpoint.base_url)
    host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
    destination = f"{url.scheme}://{host}" + (f":{url.port}" if url.port else "")
    return {"destination": destination, "id": choice.id, "provider": choice.provider.name, "endpoint": choice.endpoint.eid,
            "model": choice.model, "wire_model": choice.provider.api_model(choice.model),
            "protocol": choice.spec.protocol.label or "chat_completions",
            "contract_digest": digest}


def recheck_model(choice: providers.Choice) -> None:
    if model_receipt(select_model(choice.id)) != model_receipt(choice):
        raise ValueError("model configuration changed while queued; preview again")


def model_route(choice: providers.Choice) -> route_mod.Route:
    """This endpoint or nothing: a host-selected model never rides a gateway."""
    try:
        return route_mod.bind(choice.provider, choice.endpoint, choice.model, gateway_ok=False)
    except route_mod.RouteError:
        raise ValueError(f"missing credential for {choice.endpoint.eid}; configure its Rocky-owned key") from None


def model_key(choice: providers.Choice) -> str:
    return model_route(choice).key


def model_client(choice: providers.Choice):
    return route_mod.open_client(model_route(choice), max_retries=0, timeout=120.0)


def require_usd_price(choice: providers.Choice, ledger) -> None:
    if choice.provider.local:
        ledger.mark_free(choice.model)
        return
    rates = (ledger.pricing["models"].get(choice.model) or {}).get("usd")
    if not rates or any(not isinstance(rates.get(k), (int, float)) or
                        not math.isfinite(rates[k]) or rates[k] < 0
                        for k in ("in_hit", "in_miss", "out")):
        raise ValueError(f"USD budget/accounting unavailable for {choice.id}; configure USD prices first")


async def text_completion(client, choice: providers.Choice, messages: list[dict],
                          max_tokens: int, *, ledger=None) -> str:
    """A bounded text-only request; native effort/sampling/wire ID stay per model."""
    p, model = choice.provider, choice.model
    protocol = choice.spec.protocol
    body = build_extra_body(False, "low", p.reasoning,
                            efforts=p.efforts_for(model), thinking_off=p.thinking_off_for(model))
    # Off means the lowest native tier for models that cannot disable thinking.
    thinking = not p.thinking_off_for(model)
    try:
        response = await client.chat.completions.create(
            model=p.api_model(model), messages=protocol.messages(messages, tools=False),
            **protocol.output_cap(min(max_tokens, choice.spec.max_output)), stream=False,
            extra_body=body, **protocol.sampling(thinking))
    except Exception as exc:
        # SDK error bodies may echo private prompts/credentials or upstream URLs.
        raise RuntimeError(f"model request failed for {choice.id} ({type(exc).__name__})") from None
    if ledger is not None and response.usage is None:
        raise RuntimeError(f"usage unavailable from {choice.id}; cannot account for Goal budget")
    if ledger is not None and response.usage is not None:
        usage = response.usage.model_dump()
        hit = providers.cache_hit_tokens(usage, p.cache_field)
        if hit is not None:
            usage["prompt_cache_hit_tokens"] = hit
        ledger.add(model, usage)
    if not response.choices:
        raise RuntimeError(f"empty response from {choice.id}")
    result = response.choices[0]
    message = result.message
    problem = protocol.completion_error(result.finish_reason, tool_calls=bool(message.tool_calls))
    if problem or result.finish_reason != "stop" or message.tool_calls or not (message.content or "").strip():
        raise RuntimeError(f"incomplete or non-text response from {choice.id}")
    return message.content.strip()
