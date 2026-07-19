"""rocky's effort dial: off | high | xhigh | max.

The dial is rocky-owned and provider-neutral. Providers name their tiers
differently — DeepSeek V4 accepts only high|max, OpenAI-style models use
low|medium|high — so each provider gets a clamp table here and the dial value
never goes on the wire directly. Unknown values pass through untouched, so a
future model registry can bring its own tiers without touching callers.

`off` never reaches a request body: it means thinking disabled, and callers
keep the last effort so switching back on restores it.
"""
from __future__ import annotations

EFFORT_LEVELS = ("off", "high", "xhigh", "max")  # the user-facing dial
CLI_EFFORTS = ("high", "xhigh", "max")  # flag values (`off` = --no-thinking)

# Per-reasoning-policy clamp: the dial value → what this provider accepts.
# DeepSeek V4 knows only high|max (xhigh clamps to max); OpenAI-style maps the
# rocky dial to low|medium|high. A provider's profile names its policy.
_CLAMP = {
    "deepseek": {"high": "high", "xhigh": "max", "max": "max"},
    "openai": {"high": "medium", "xhigh": "high", "max": "high"},
}


def to_deepseek(effort: str) -> str:  # kept for existing callers
    return _CLAMP["deepseek"].get(effort, effort)


def build_extra_body(thinking: bool, effort: str, reasoning: str = "deepseek") -> dict:
    """The reasoning fields for this request, shaped by the provider's policy:

    - "deepseek": `{thinking: {type}, reasoning_effort}` (DeepSeek's own knob)
    - "openai":   bare `{reasoning_effort}` (OpenAI-style), only when thinking
    - "none":     nothing — the provider has no reasoning param

    The OpenAI SDK carries these via `extra_body`; unknown keys a provider
    ignores are harmless, but "none" keeps the body clean for strict endpoints.
    """
    if not thinking or reasoning == "none":
        return {"thinking": {"type": "disabled"}} if reasoning == "deepseek" else {}
    clamp = _CLAMP.get(reasoning, {})
    tier = clamp.get(effort, effort)
    if reasoning == "deepseek":
        return {"thinking": {"type": "enabled"}, "reasoning_effort": tier}
    return {"reasoning_effort": tier}
