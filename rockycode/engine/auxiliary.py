"""Model-aware request settings for short, text-only background jobs.

The caller owns the client/endpoint. Resolving metadata must never move an
existing client's credentials or a session's contents to another endpoint.
"""
from __future__ import annotations

from rockycode.engine import providers as P
from rockycode.engine.effort import build_extra_body


def request_options(model: str, max_tokens: int) -> dict:
    resolved = P.resolve(model)
    if resolved is None:
        # Unknown OpenAI-compatible servers get no vendor-specific parameters.
        return {"model": model, "max_tokens": max_tokens}
    provider, _, canonical = resolved
    spec = provider.spec(canonical)
    can_disable = provider.thinking_off_for(canonical)
    # Always-thinking models count reasoning against this same output budget.
    # A 24-token title or a 256-token memory decision otherwise has no room
    # for an answer. This is a bounded allowance, not a guarantee of completion.
    budget = max_tokens if can_disable else max(max_tokens, 4096)
    return {
        "model": provider.api_model(canonical),
        **spec.protocol.output_cap(min(budget, spec.max_output)),
        "extra_body": build_extra_body(False, "low", provider.reasoning,
                                        efforts=provider.efforts_for(canonical),
                                        thinking_off=can_disable),
        **spec.protocol.sampling(not can_disable),
    }


def response_text(response) -> str:
    """Never turn truncated structured output into a stored memory decision."""
    if not response.choices:
        raise ValueError("empty auxiliary response")
    choice = response.choices[0]
    if choice.finish_reason != "stop" or getattr(choice.message, "tool_calls", None):
        raise ValueError("incomplete auxiliary response")
    return (choice.message.content or "").strip()
