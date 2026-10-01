"""rocky's effort dial: off | low | high | max.

The dial is rocky-owned and provider-neutral. Providers name their tiers
differently — DeepSeek / GLM / Kimi take low|high|max, OpenAI-style models
(StepFun) low|medium|high, MiniMax has no tiers at all — so the dial value
never goes on the wire directly: it is clamped onto the provider's own tier
list (registry `efforts`, or the wire shape's default) per call.

`off` never reaches a request body as an effort: it means thinking disabled,
and callers keep the last effort so switching back on restores it. A provider
whose thinking can't be turned off (`thinking_off = false`: GLM, Kimi) gets
its LOWEST tier instead — off still means "as little thinking as it allows".

`xhigh` (the pre-V4.1 dial) is still accepted and means max.
"""
from __future__ import annotations

from typing import Optional

EFFORT_LEVELS = ("off", "low", "high", "max")  # the user-facing dial
CLI_EFFORTS = ("low", "high", "max")  # flag values (`off` = --no-thinking)
LEGACY_EFFORTS = {"xhigh": "max"}     # accepted, mapped — old configs keep working

# Default tier lists per wire shape (the registry's `efforts` overrides them).
POLICY_TIERS: dict[str, tuple[str, ...]] = {
    "thinking": ("low", "high", "max"),
    "effort": ("low", "high", "max"),
    "openai": ("low", "medium", "high"),
}


def normalize(effort: str) -> str:
    return LEGACY_EFFORTS.get(effort, effort)


def clamp(effort: str, tiers) -> str:
    """The dial value → this provider's tier, by POSITION: low = its lowest,
    max = its highest, high = the middle one (OpenAI-style low|medium|high
    gets medium for rocky's high — max is the top, as the dial promises).
    A provider whose tiers ARE the dial passes names through; a value that
    isn't a dial word (someone typing `medium`) passes through untouched,
    so a future provider can bring its own tiers."""
    effort = normalize(effort)
    tiers = tuple(tiers or ())
    if not tiers or tiers == CLI_EFFORTS:
        return effort
    if effort == "low":
        return tiers[0]
    if effort == "max":
        return tiers[-1]
    if effort == "high":
        return tiers[(len(tiers) - 1) // 2]
    return effort


def _tiers(reasoning: str, efforts) -> tuple[str, ...]:
    policy = "thinking" if reasoning == "deepseek" else reasoning
    if efforts is not None:
        return tuple(efforts)
    return POLICY_TIERS.get(policy, ())


def wire_tier(effort: str, reasoning: str = "thinking", efforts=None) -> Optional[str]:
    """What reasoning_effort this provider would receive for the dial value —
    None when the shape carries no effort field (minimax, none, mimo)."""
    tiers = _tiers(reasoning, efforts)
    return clamp(effort, tiers) if tiers else None


def to_deepseek(effort: str) -> str:  # kept for existing callers
    return clamp(effort, POLICY_TIERS["thinking"])


def build_extra_body(thinking: bool, effort: str, reasoning: str = "thinking", *,
                     efforts=None, thinking_off: bool = True) -> dict:
    """The reasoning fields for this request, shaped by the provider's wire
    shape (registry `reasoning`):

    - "thinking":        `{thinking: {type}, reasoning_effort}` (DeepSeek, GLM, MiMo)
    - "effort":          bare `{reasoning_effort}`, thinking always on (Kimi)
    - "enable_thinking": `{enable_thinking: bool}` (Qwen / DashScope)
    - "minimax":         `{thinking: {type: adaptive|disabled}, reasoning_split}`
    - "openai":          bare `{reasoning_effort}` low|medium|high, only when thinking
    - "none":            nothing — the provider has no reasoning param

    `efforts` = the provider's own tier list (None → the shape's default,
    () → no effort field); `thinking_off=False` = the model can't disable
    thinking, so `off` sends the lowest tier. The OpenAI SDK carries these
    via `extra_body`; "none" keeps the body clean for strict endpoints.
    """
    policy = "thinking" if reasoning == "deepseek" else reasoning
    tiers = _tiers(policy, efforts)
    tier = clamp(effort, tiers) if tiers else None
    if policy == "none":
        return {}
    if policy == "thinking":
        if not thinking:
            if thinking_off:
                return {"thinking": {"type": "disabled"}}
            body: dict = {"thinking": {"type": "enabled"}}
            if tiers:
                body["reasoning_effort"] = tiers[0]
            return body
        body = {"thinking": {"type": "enabled"}}
        if tier:
            body["reasoning_effort"] = tier
        return body
    if policy == "effort":
        if not thinking:
            return {"reasoning_effort": tiers[0]} if tiers else {}
        return {"reasoning_effort": tier} if tier else {}
    if policy == "openai":
        if not thinking:
            return {} if thinking_off else ({"reasoning_effort": tiers[0]} if tiers else {})
        return {"reasoning_effort": tier} if tier else {}
    if policy == "enable_thinking":
        return {"enable_thinking": bool(thinking)}
    if policy == "minimax":
        if not thinking:
            return {"thinking": {"type": "disabled"}}
        return {"thinking": {"type": "adaptive"}, "reasoning_split": True}
    return {}
