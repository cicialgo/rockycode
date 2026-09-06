"""Engine events: the single contract between the agent core and everything
that watches it (TUI transcript, Rocky pet, trajectory logger, game layer).

Subscribers must tolerate unknown event types — match on isinstance and
ignore what you don't handle, so new events never break old UIs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AgentState(str, Enum):
    """What Rocky is doing right now. Drives the pet animation."""

    IDLE = "idle"
    THINKING = "thinking"
    RESPONDING = "responding"
    TOOL = "tool"
    COMPACTING = "compacting"
    AMAZED = "amazed"
    ERROR = "error"


@dataclass
class Event:
    pass


@dataclass
class StateChanged(Event):
    state: AgentState


@dataclass
class TurnStarted(Event):
    user_message: str


@dataclass
class ThinkingDelta(Event):
    """A chunk of DeepSeek's reasoning_content stream."""

    text: str


@dataclass
class TextDelta(Event):
    """A chunk of the assistant's visible reply."""

    text: str


@dataclass
class ToolStarted(Event):
    call_id: str
    tool: str
    args: dict = field(default_factory=dict)


@dataclass
class ToolFinished(Event):
    call_id: str
    tool: str
    output: str
    ok: bool
    duration_s: float


@dataclass
class Compacted(Event):
    """History was rewritten to fit the context window (mid-turn)."""

    strategy: str  # "prune" (old tool outputs stubbed) | "summarize" (state doc rebuild)
    tokens_before: int  # projected prompt tokens that tripped the limit
    tokens_after: int  # conservative estimate of the rebuilt history
    messages_before: int
    messages_after: int


@dataclass
class ContextReminder(Event):
    """Soft, non-blocking nudge: context passed the 'model degrades past here'
    mark (DeepSeek V4 ~50%). The user decides whether to /clear; auto-compaction
    only kicks in near the window ceiling."""

    pct: float   # fraction of the window currently used
    window: int


@dataclass
class CacheReset(Event):
    """The provider's prompt cache stopped covering our prefix WITHOUT rocky
    changing it — measured from the API's own per-request hit tokens, never
    estimated. Emitted only for surprise drops (upstream eviction); resets
    rocky causes itself (model switch / compaction / mode change) are logged
    to the trajectory but stay silent."""

    hit_tokens: int       # what this request actually hit (API-reported)
    expected_tokens: int  # ≈ previous request's prompt, 64-token-block floor
    idle_s: float         # gap since the previous request — the eviction clue


@dataclass
class TurnFinished(Event):
    """End of a full user turn (all tool round-trips done)."""

    steps: int
    usage: dict = field(default_factory=dict)


@dataclass
class EngineError(Event):
    message: str
