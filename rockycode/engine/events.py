"""Engine events: the single contract between the agent core and everything
that watches it (TUI transcript, Rocky pet, trajectory logger, game layer).

Subscribers must tolerate unknown event types — match on isinstance and
ignore what you don't handle, so new events never break old UIs.
"""
from __future__ import annotations

from contextvars import ContextVar
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
    turn: str = ""  # the engine's id for this logical turn — Envelope.turn on its calls


@dataclass
class ThinkingDelta(Event):
    """A chunk of DeepSeek's reasoning_content stream."""

    text: str


@dataclass
class TextDelta(Event):
    """A chunk of the assistant's visible reply."""

    text: str


@dataclass
class Envelope:
    """Who a tool call is, apart from the provider's call id (docs/design/call-identity.md).

    A sidecar: it never enters the model request. It rides on ToolStarted /
    ToolFinished, on serve / exec receipts and on the per-call trajectory note, so
    a receipt can be tied to the turn, the model request and the catalog that
    produced it. Consumers that only know call_id keep working.
    """

    session: str = ""             # TrajectoryLogger.session_id; a history fork is a new session
    turn: str = ""                # one logical user turn; a bounded continuation keeps it
    request: str = ""             # one admitted model request
    attempt: int = 1              # physical sends that request took (SDK retries, when observable)
    logical_call_id: str = ""     # host-minted, unique across turns and sessions
    provider_call_id: str = ""    # the model's tool_call id — may legally recur across turns
    registry_generation: int = 0  # the catalog generation the request was built against
    route: dict = field(default_factory=dict)  # resolved endpoint / model / protocol, never a key


# The envelope of the tool call running in this task, for a tool that spawns
# work of its own (explore, a branch review) to record its parent lineage.
CURRENT_CALL: ContextVar[Envelope | None] = ContextVar("rockycode_current_call", default=None)


@dataclass
class ToolStarted(Event):
    call_id: str
    tool: str
    args: dict = field(default_factory=dict)
    envelope: Envelope = field(default_factory=Envelope)


@dataclass
class ToolFinished(Event):
    call_id: str
    tool: str
    output: str
    ok: bool
    duration_s: float
    envelope: Envelope = field(default_factory=Envelope)


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
    mark (~50% of the window). The user decides whether to /clear;
    auto-compaction only kicks in near the window ceiling."""

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
    turn: str = ""


@dataclass
class EngineError(Event):
    message: str
    # A failed model request also says what kind of failure it was (engine/errors.py),
    # carries the facts a front end may word for itself, and keeps the SDK's own
    # text as `detail`. Empty for every other error.
    kind: str = ""
    detail: str = ""
    params: dict = field(default_factory=dict)
