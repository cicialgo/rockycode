"""The agent loop: stream an OpenAI-compatible model with native tool calls,
execute tools, repeat until the model answers without tools. Emits Events;
owns history.

Before every API call the loop projects the next prompt size (last real
prompt_tokens + char-based estimates for newer messages) and, over the
threshold, compacts history via compaction.py (prune → state summary).

Model-specific facts are NOT here: the context window, output cap, vision
flag, reasoning wire shape and cache field all come from the provider
registry (engine/providers.py ← rockycode/models.toml) as a ModelSpec, so a
new model is a data edit. What the loop does assume of every provider:
- reasoning params ride `extra_body` (effort.build_extra_body shapes them)
- the stream carries delta.reasoning_content AND delta.content
- reasoning history and sampling follow an explicit per-model protocol
- usage extras (cache hit/miss) are read via model_dump()
"""
from __future__ import annotations

import asyncio
import copy
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import asdict
import hashlib
import json
import re
import time
import uuid
from urllib.parse import urlsplit
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Optional

import httpx
from openai import AsyncOpenAI

from rockycode.onboarding import current_key_source, require_base_url

from rockycode.engine.model_protocol import GENERIC
from rockycode.engine import compaction
from rockycode.engine import errors as errors_mod
from rockycode.engine import images as images_mod
from rockycode.engine import planmode
from rockycode.engine.planning import PlanningSession, build_plan_tools
from rockycode.engine import providers as providers_mod
from rockycode.engine import route as route_mod
from rockycode.engine import tools as tools_mod
from rockycode.engine import effort as effort_mod
from rockycode.engine.approval import Refusal
from rockycode.engine.effort import build_extra_body
from rockycode.engine.file_access import FileAccessPolicy
from rockycode.engine.redact import redact
from rockycode.engine.events import (
    CURRENT_CALL,
    AgentState,
    CacheReset,
    Compacted,
    ContextReminder,
    EngineError,
    Envelope,
    Event,
    StateChanged,
    TextDelta,
    ThinkingDelta,
    ToolFinished,
    ToolStarted,
    TurnFinished,
    TurnStarted,
)
from rockycode.engine.outcome import SessionStats
from rockycode.engine.trajectory import TrajectoryLogger
from rockycode.prompts.rocky import ROCKY_SYSTEM
from rockycode.session import repair_history

MAX_STEPS = 100
_TURN_STOPPED = "[not run] rocky stopped this turn before this call: the user decides the parked steps first."
FINALIZE_STEPS = 10  # room to verify and report before the finite turn stops
# Fallbacks for a model the registry doesn't know; a known model brings its
# own limits (ModelSpec.context / max_output) at launch and on every switch.
CONTEXT_WINDOW = providers_mod.DEFAULT_CONTEXT
MAX_OUTPUT = providers_mod.DEFAULT_MAX_OUTPUT
# Two-tier context handling. Long-context models get sloppier well before the
# window is full (DeepSeek V4 measurably past ~50%), but forcing a compaction
# there interrupts the user — so 50% is a SOFT one-time reminder
# (ContextReminder), and automatic compaction only fires near the ceiling.
REMIND_THRESHOLD = 0.50   # non-blocking nudge: "past the half, /clear if you want"
COMPACT_THRESHOLD = 0.90  # hard auto-compact — the safety net before overflow
# Provider prefix caches are block-granular (DeepSeek: 64 tokens); the cache
# observer floors its expectation to this so a partial last block never reads
# as an eviction.
CACHE_BLOCK = 64


def budget_notice(remaining: int, *, closing: bool = False) -> str:
    """Resource information, not a demand to edit or declare success."""
    phase = "Closing window" if closing else "Resource update"
    return (
        f"[harness] {phase}: {remaining} model calls remain in this turn, including this one. "
        "Continue relevant investigation and verification within the user's scope. "
        "Prioritize the unresolved question that matters most; you do not need to "
        "finish early or make a change because of this notice. If the work cannot "
        "be completed within this limit, leave a clear account of evidence, changes, "
        "unverified work and the next step. Do not claim unverified success."
    )


def _merge_usage(total: dict[str, int], usage: dict) -> None:
    for k, v in usage.items():
        if isinstance(v, int):
            total[k] = total.get(k, 0) + v


# The model request in flight on this task, for the transport hook below:
# {"attempts": n} counts physical sends, SDK retries included.
_REQUEST: ContextVar[Optional[dict]] = ContextVar("rockycode_request", default=None)


async def _count_attempt(request) -> None:
    """httpx request hook: one call per physical send. Any other traffic on the
    same client (a compaction summary, an explore child) finds no record and is
    ignored; the loop sets one only around its own chat.completions.create."""
    record = _REQUEST.get()
    if record is not None:
        record["attempts"] += 1


async def _always_allow(name: str, args: dict) -> bool:
    """Default approver: every tool call runs, no prompt. Keeps bench/headless
    and the current TUI behavior unchanged unless a real approver is injected."""
    return True


class Engine:
    def __init__(
        self,
        model: str,
        *,
        thinking: bool = True,
        reasoning_effort: str = "max",
        # None = the model's registry value (its real max output — never
        # truncate); a number is an explicit ceiling that survives /model.
        max_tokens: Optional[int] = None,
        workdir: Optional[Path] = None,
        allowed_roots: tuple[Path, ...] = (),
        system_prompt: str = ROCKY_SYSTEM,
        client: Optional[AsyncOpenAI] = None,
        model_choice=None,  # trusted host snapshot; keeps injected transports native
        # The route this engine's requests take (engine/route.py). None without a
        # client = resolve `model` here. With a client, it is that client's route:
        # how a child or a host-opened transport brings the model's contract along.
        route: Optional[route_mod.Route] = None,
        registry: Optional[dict[str, tools_mod.Tool]] = None,
        trajectory_meta: Optional[dict] = None,
        trajectory_logger=None,  # host-owned recorder; None preserves normal persistence
        context_window: Optional[int] = None,  # None = the model's registry value
        compact_threshold: float = COMPACT_THRESHOLD,
        remind_threshold: float = REMIND_THRESHOLD,
        max_steps: int = MAX_STEPS,
        finalize_steps: int = FINALIZE_STEPS,
        approver: Optional[Callable[[str, dict], Awaitable[bool]]] = None,
    ) -> None:
        self.model = model
        self._host_selected_model = model_choice is not None
        # Friendly registry id stays on self.model (pricing, /model, trajectory).
        # self.wire_model is what chat.completions receives; adopt() replaces it
        # when the spec spells a different API id.
        self.wire_model = model
        self.thinking = thinking
        self.reasoning_effort = effort_mod.normalize(reasoning_effort)
        # The active provider's reasoning WIRE SHAPE + its own effort tiers +
        # whether thinking can be switched off + where usage reports cache
        # hits — all registry data (providers.Provider), adopted at launch and
        # on every /model switch. Defaults = the home provider's shape.
        self.protocol = GENERIC
        self.reasoning_policy = "thinking"
        self.efforts: Optional[tuple[str, ...]] = None
        self.thinking_off = True
        self.cache_field = "prompt_cache_hit_tokens"
        self.provider_name = "deepseek"
        self.tools_enabled = True  # profile tools:off → drop tool schemas
        # Serving on the user's OWN machine (ollama etc.)? Local compat layers
        # ignore tool_choice, so compaction's summarize call must not carry
        # tool schemas — the model may answer with a tool call instead of a
        # summary.
        self.provider_local = False
        # Does the active model take image input? False for DeepSeek (home
        # model), so plain sessions send byte-identical requests; flipped by
        # switch_provider for a vision profile (minimax / kimi / stepfun).
        # Gates images.api_view at the API boundary: True inflates image_path
        # parts to base64 data URLs, False collapses them to text placeholders.
        self.vision_enabled = False
        # Limits: an explicit number (flag / env / config / /config) is the
        # user's own ceiling and survives model switches; otherwise the active
        # model's registry entry decides, and a switch re-paces them.
        self._explicit_limits = {"context_window": bool(context_window),
                                 "max_tokens": bool(max_tokens)}
        self.model_max_output = MAX_OUTPUT
        self.max_tokens = max_tokens or MAX_OUTPUT
        self.context_window = context_window or CONTEXT_WINDOW
        self.compact_threshold = compact_threshold
        self.remind_threshold = remind_threshold
        self._reminded = False  # fired the soft 50% nudge? re-arms below the mark
        self.max_steps = max_steps
        self.finalize_steps = finalize_steps
        self._pending_turn_state = None
        # Opt-in approval gate. Default = always-allow, so bench/headless and the
        # current TUI stay unchanged until a real approver is injected (the TUI
        # sets one that runs permission.decide() + a modal). Used by _approve.
        self.approver = approver or _always_allow
        # Plan mode: set to the session's plan-file path to make this engine
        # read-only — planmode.gate() then runs on every tool call BEFORE the
        # approver (the plan file itself is the one writable target). None = off.
        # Host state only; the model never toggles it (docs/plan-mode-design.md).
        self.planning = PlanningSession(self)
        self._planning_tools_enabled = False
        # Resolved paths a live human approval widened file reads to. Mutable
        # and held by reference in the registry, so approval takes effect live.
        # Never fed by a file — only a launch flag or a click.
        self.read_grants: set[Path] = set()
        # Compaction bookkeeping: the API's real prompt size for everything
        # up to history[_sent_until], estimates for anything appended after.
        self._last_prompt_tokens = 0
        self._sent_until = 0
        # Cache observability (docs/vision-providers-design.md): the previous
        # request's (prompt, hit) straight from the API's usage numbers, when
        # it was sent, and — when rocky itself just forfeited the prefix
        # (model switch / compaction / mode change) — why, so the eviction
        # detector stays quiet about resets we caused ourselves.
        self._cache_prev: Optional[tuple[int, int]] = None
        self._cache_req_at: Optional[float] = None
        self._cache_reset_reason: Optional[str] = None
        # Heuristic outcome counters (self-evolve phase 0), incremented at the
        # exact branch points below and flushed as ONE `outcome` record by
        # finalize_outcome() when the session ends.
        self.stats = SessionStats()
        self._outcome_written = False
        # Boundary hooks (duck-typed): on_boundary(engine, reason=, history_view=)
        # is called sync at a turn end / before a compaction / at a step-limit
        # exit; on_final(engine, timeout=) is awaited once by the exit paths;
        # cancel() drops in-flight work. Empty by default — bench never
        # attaches one. Reflect (dream/reflect.py) is the one hook today.
        self.hooks: list = []
        self.workdir = workdir or Path.cwd()
        self.allowed_roots = allowed_roots
        self.file_access = (FileAccessPolicy(self.workdir, allowed_roots, self.read_grants)
                            if registry is None else None)
        # Launch model: when the engine builds its OWN client, engine/route.py
        # decides where the model's requests go and with which key, and a
        # registry model brings its reasoning shape / tools / vision along from
        # step one. A missing key raises RouteError here: nothing is ever sent
        # to another provider instead. An INJECTED client with no route is
        # never second-guessed: the caller owns the transport and its contract.
        if model_choice is not None and client is None:
            raise ValueError("model_choice requires its explicitly bound client")
        self.route = route
        launch = None
        if model_choice is not None:
            launch = (model_choice.provider, model_choice.endpoint, model_choice.model)
        else:
            if self.route is None and client is None:
                self.route = route_mod.route(self.model)
            if self.route is not None and self.route.known:
                launch = (self.route.provider, self.route.endpoint, self.route.model)
        if launch is not None:
            self.adopt(*launch)
        self.client = client or route_mod.open_client(self.route)
        # Call identity (docs/design/call-identity.md, phase 1): the catalog
        # generation stamped on every request and receipt, the logical turn in
        # progress, and whether the transport reports its physical sends.
        self.registry_generation = 0
        self._turn_id = ""
        self._instrumented = self._instrument(self.client)
        self.registry = (
            registry if registry is not None
            else tools_mod.build_registry(self.workdir, allowed_roots, read_grants=self.read_grants,
                                          vision_active=lambda: self.vision_enabled, file_access=self.file_access)
        )
        self.history: list[dict] = [{"role": "system", "content": system_prompt}]
        # Collaboration mode (host-owned, like plan_file — the model never
        # toggles it): set_mode() swaps a contract into the system prompt.
        self._base_system = system_prompt
        self._mode_contract: Optional[str] = None
        self.mode_name: Optional[str] = None
        self.resumed_mode: Optional[str] = None  # mode seen in a resumed session's prompt
        self.trajectory = trajectory_logger if trajectory_logger is not None else TrajectoryLogger(
            meta={
                "model": self.model,
                "provider": self.provider_name,
                "reasoning": self.reasoning_policy,
                "thinking": thinking,
                "reasoning_effort": self.reasoning_effort,
                "max_tokens": self.max_tokens,
                "context_window": self.context_window,
                "max_steps": max_steps,
                "workdir": str(self.workdir),
                "base_url": self.route.base_url if self.route else require_base_url(),
                **(trajectory_meta or {}),
            }
        )
        self.trajectory.message(self.history[0])

    @property
    def plan_file(self) -> Optional[Path]:
        return self.planning.path if self.planning.active else None

    @plan_file.setter
    def plan_file(self, path: Optional[Path]) -> None:
        if path is None:
            self.planning.leave()
        else:
            self.planning.attach(path)

    def enable_planning(self) -> None:
        """Register interactive planning once, without changing bench/headless tools."""
        if not self._planning_tools_enabled:
            self._planning_tools_enabled = True
            self.registry.update(build_plan_tools(self))

    def resume(self, messages: list[dict], *, from_session: Optional[str] = None,
               planning_state: Optional[dict] = None) -> list[dict]:
        """Seed history from a prior session's messages. Keeps the CURRENT
        system prompt (so updated memory/skills/instructions apply) and carries
        the rest of the conversation. The carried messages are logged into the
        new trajectory, so it stays self-contained for training."""
        # A prior session's mode lives in ITS system prompt, which we drop —
        # remember the name so the UI can offer re-entry instead of silence.
        self.resumed_mode = None
        for m in messages:
            if m.get("role") == "system":
                for line in str(m.get("content", "")).splitlines():
                    if line.startswith("# Collaboration mode: "):
                        self.resumed_mode = line.removeprefix("# Collaboration mode: ").strip()
        self.artifact_resumed_from = from_session
        registry = getattr(self, "artifact_registry", None)
        if registry is not None and from_session:
            registry.resume_from(from_session)
        self.planning.restore(planning_state)
        carried = [m for m in messages if m.get("role") != "system"]
        for m in carried:
            self._append(m)
        self.trajectory.note({"resumed_from": from_session, "carried": len(carried)})
        return carried

    def swap_registry(self, registry: dict[str, tools_mod.Tool], *,
                      file_access: FileAccessPolicy | None = None) -> None:
        """Swap tools and their host-owned read policy as one capability set.

        A custom/sandbox registry never implicitly inherits local YOLO. A host
        restoring its own registry must explicitly restore its matching policy.
        """
        self.registry = registry
        self.file_access = file_access
        if self._planning_tools_enabled:
            self.registry.update(build_plan_tools(self))
        self.bump_registry("swap", tools=sorted(self.registry))

    def bump_registry(self, reason: str, *, tools=()) -> int:
        """Advance the catalog generation after the tool set changed. Hosts go
        through Runtime.publish_registry; swap_registry calls this itself. The
        number is stamped on every request and receipt, so a receipt names the
        catalog the model was offered. Phase 1 records; phase 2 binds."""
        self.registry_generation += 1
        self.trajectory.note({"registry": {"generation": self.registry_generation,
                                           "reason": reason, "tools": list(tools)}})
        return self.registry_generation

    @staticmethod
    def _instrument(client) -> bool:
        """Count physical sends on the SDK's httpx client, so an SDK retry is an
        observable attempt rather than a silent re-spend. A transport that is
        not httpx (a fake, another SDK) stays uninstrumented: attempts then read
        as the one send we know of. Idempotent — explore children share the
        parent's client."""
        http = getattr(client, "_client", None)
        if not isinstance(http, httpx.AsyncClient):
            return False
        hooks = http.event_hooks["request"]
        if _count_attempt not in hooks:
            hooks.append(_count_attempt)
        return True

    def _route(self) -> dict:
        # The resolved route, never the key: endpoint, model and wire protocol
        # this request is bound to (a /model switch changes all three).
        return {"endpoint": self.provider_name, "model": self.model, "protocol": self.protocol.label}

    async def _execute(self, env: Envelope, name: str, arguments_json: str) -> tuple[str, bool]:
        # The envelope is visible to the tool while it runs (CURRENT_CALL), so a
        # child it spawns can record its parent turn / request / call.
        token = CURRENT_CALL.set(env)
        try:
            return await tools_mod.execute(self.registry, name, arguments_json)
        finally:
            CURRENT_CALL.reset(token)

    def _receipt(self, env: Envelope, tool: str, receipt: str, duration_s: float) -> None:
        # One trajectory line per logical call, written when its result is
        # known: ok / error (it ran), refused (a gate or the user), unknown
        # (cancelled after dispatch — the effect may have happened), cancelled
        # (never dispatched). executed_generation differs from the envelope's
        # when the catalog changed between request and dispatch — the mid-turn
        # swap phase 2 closes; phase 1 makes it visible.
        self.trajectory.note({"call": {**asdict(env), "tool": tool, "receipt": receipt,
                                       "duration_s": round(duration_s, 3),
                                       "executed_generation": self.registry_generation}})

    def _note_request(self, request_id: str, step: int, sends: dict, generation: int,
                      route: dict, *, tools, error: Optional[str] = None) -> int:
        # One trajectory line per admitted model request. attempts counts the
        # physical sends when the transport is instrumented; otherwise it is
        # the one send we know of and attempts_observed says so.
        observed = self._instrumented and sends["attempts"] > 0
        attempts = sends["attempts"] if observed else 1
        self.trajectory.note({"request": {
            "turn": self._turn_id, "request": request_id, "step": step,
            "attempts": attempts, "attempts_observed": observed,
            "registry_generation": generation, "route": route, "tools": len(tools or []),
            **({"error": error} if error else {}),
        }})
        return attempts

    def rollback(self, n: int, *, reason: str = "") -> int:
        """Drop everything after the first *n* messages — a turn boundary the
        caller recorded before starting a turn it now wants gone from LIVE
        context (a quiet /loop tick: it carried no information, and V4
        degrades past half its window). Context hygiene only: the trajectory
        is append-only and keeps every record, plus a note here, so training
        data and --resume see the full story (resume replays it, exactly as a
        compaction is replayed today). Cache-neutral — the prefix before *n*
        is unchanged, so the next request still hits. Returns how many
        messages were dropped; 0 (a no-op) when *n* is not a proper prefix."""
        if n < 1 or n >= len(self.history):
            return 0
        dropped = len(self.history) - n
        del self.history[n:]
        # The projection's "already measured" cursor must not point past the
        # end. _last_prompt_tokens still counts the dropped tail — an
        # over-estimate, the safe direction; the next call re-measures.
        if self._sent_until > n:
            self._sent_until = n
        self.trajectory.note({"rollback": {"to": n, "dropped": dropped, "reason": reason}})
        return dropped

    def flush_to(self, seed: list[dict], *, reason: str = "") -> int:
        """Drop the running conversation back to `seed` (context-inertia fix for goal mode).

        Keeps the CURRENT system prompt (history[0]). Prefix intact -> truncate in place
        (cache-neutral like rollback()). Prefix rewritten (compaction happened) -> replace with
        deepcopy(seed[1:]) and mark a cache reset. Zero projection counters. Write a `compaction`
        trajectory record with strategy "flush" + new_history so load_history/--resume replays it.
        Returns messages dropped; 0 = no-op.
        """
        if not seed or seed[0].get("role") != "system":
            raise ValueError("flush_to: seed must start with the system message")
        n = len(seed)
        intact = len(self.history) >= n and self.history[1:n] == seed[1:]
        if intact and len(self.history) == n:
            return 0
        self._boundary(reason="compaction")
        before = len(self.history)
        if intact:
            del self.history[n:]
            cache_kept = True
        else:
            del self.history[1:]
            self.history.extend(copy.deepcopy(seed[1:]))
            self._mark_cache_reset("context flush")
            cache_kept = False
        self._last_prompt_tokens = 0
        self._sent_until = 0
        self._reminded = False
        self.trajectory.compaction({
            "strategy": "flush",
            "reason": reason,
            "messages_before": before,
            "messages_after": len(self.history),
            "cache_kept": cache_kept,
            "new_history": copy.deepcopy(self.history),
        })
        return before - len(self.history)

    def finalize_outcome(self) -> Optional[dict]:
        """Write the session's heuristic outcome record (self-evolve phase 0).

        Called once at session end — the app's exit flow, or cli after the
        app closes. Idempotent, and skipped when no user turn ever ran, so an
        open-and-close session doesn't grow a meaningless reward line. The
        graded outcome (source="reflect") is appended right after, by
        the reflect hook's final call — or much later by the dream's cold reflect.
        """
        if self._outcome_written or self.stats.turns == 0:
            return None
        self._outcome_written = True
        data = {"source": "heuristic", **self.stats.as_data()}
        self.trajectory.outcome(data)
        return data

    # ---- boundary hooks -------------------------------------------------------

    def _boundary(self, *, reason: str = "messages",
                  history_view: Optional[list[dict]] = None) -> None:
        """A turn ended, a compaction is about to rewrite history, or the step
        limit stopped the turn. Hooks see it synchronously and may schedule
        their own tasks; a hook must never break a turn."""
        for hook in list(self.hooks):
            try:
                hook.on_boundary(self, reason=reason, history_view=history_view)
            except Exception:  # noqa: BLE001 — observers never break the loop
                pass

    async def run_final_hooks(self, timeout: float = 15.0) -> list:
        """Awaited once by every exit path (chat exit flow, exec / goal / serve
        teardown). Returns one result per hook; a hook that raises yields None."""
        out = []
        for hook in list(self.hooks):
            try:
                out.append(await hook.on_final(self, timeout=timeout))
            except Exception:  # noqa: BLE001
                out.append(None)
        return out

    async def cancel_hooks(self) -> None:
        """Teardown of a cancelled host: drop whatever the hooks have in flight."""
        for hook in list(self.hooks):
            cancel = getattr(hook, "cancel", None)
            if cancel is not None:
                try:
                    await cancel()
                except Exception:  # noqa: BLE001
                    pass

    def set_base_system(self, text: str) -> None:
        """Finalize the base system prompt after startup composition.

        The chat CLI can only generate the "# Tools this session" section
        once every tool has registered (skills/memory/web/artifact/goal/
        explore land AFTER Engine construction), so the final prompt —
        tools + language + environment + date — is set here, before the
        first user turn. Re-layers an active mode contract; logs the final
        prompt to the trajectory so training data records what was actually
        sent (the construction-time record is superseded, last-system-wins).
        """
        self._base_system = text
        if self.mode_name is not None and self._mode_contract is not None:
            self.history[0]["content"] = (
                f"{text}\n\n# Collaboration mode: {self.mode_name}\n\n"
                f"{self._mode_contract.strip()}"
            )
        else:
            self.history[0]["content"] = text
        self.trajectory.message(self.history[0])
        self.trajectory.note({"system_finalized": len(text)})
        self.planning.sync_prompt()

    def set_mode(self, name: str, contract: str) -> None:
        """Swap a collaboration contract into the system prompt (a REAL swap,
        not an appended note — models weight the system prompt hardest). Costs
        one prefix-cache miss at the switch, which the user accepted; switching
        at launch (config `mode`) costs nothing. One mode at a time: setting a
        new one replaces the old."""
        self._mark_cache_reset("mode change")
        self.history[0]["content"] = (
            f"{self._base_system}\n\n# Collaboration mode: {name}\n\n{contract.strip()}"
        )
        self.mode_name = name
        self._mode_contract = contract
        self.trajectory.note({"mode": name})
        self.planning.sync_prompt()

    def clear_mode(self) -> None:
        """Back to the plain rocky contract."""
        if self.mode_name is None:
            return
        self._mark_cache_reset("mode change")
        self.history[0]["content"] = self._base_system
        self.mode_name = None
        self._mode_contract = None
        self.trajectory.note({"mode": None})
        self.planning.sync_prompt()

    def _api_messages(self, *, tools: bool | None = None) -> list[dict]:
        if tools is None:
            tools = self.tools_enabled and bool(self.registry)
        return self.protocol.messages(
            images_mod.api_view(self.history, vision=self.vision_enabled), tools=tools)

    def _extra_body(self) -> dict:
        # self.reasoning_effort holds the dial value (low|high|max) and is
        # mutable mid-session (/effort); the provider clamp + param shape happen
        # per call, keyed by the active provider's reasoning wire shape.
        return build_extra_body(self.thinking, self.reasoning_effort, self.reasoning_policy,
                                efforts=self.efforts, thinking_off=self.thinking_off)

    def adopt(self, prov, ep, model: str) -> None:
        """Take on a registry (provider, endpoint, model): its reasoning wire
        shape, effort tiers, cache field, tools/vision flags, and — unless the
        user pinned a number — the model's own context window and output cap.
        The transport (self.client) is the caller's business."""
        spec = prov.spec(model)
        self.model = model
        self.wire_model = prov.api_model(model)
        self.protocol = spec.protocol
        self.model_max_output = spec.max_output
        self.provider_name = ep.eid
        self.reasoning_policy = prov.reasoning
        self.efforts = prov.efforts_for(model)
        self.thinking_off = prov.thinking_off_for(model)
        self.cache_field = prov.cache_field
        self.tools_enabled = prov.tools == "native"
        self.vision_enabled = providers_mod.Choice(prov, ep, model).vision
        self.provider_local = prov.local
        if not self._explicit_limits["context_window"]:
            self.context_window = spec.context
        if not self._explicit_limits["max_tokens"]:
            self.max_tokens = spec.max_output

    def set_limit(self, name: str, value: Optional[int]) -> None:
        """Pin (a positive number) or unpin (0/None → back to the active
        model's registry value) context_window / max_tokens — the /config
        path. Pinned values survive /model switches."""
        assert name in ("context_window", "max_tokens")
        if value:
            self._explicit_limits[name] = True
            setattr(self, name, int(value))
            return
        self._explicit_limits[name] = False
        hit = providers_mod.resolve(f"{self.provider_name}:{self.model}")
        spec = hit[0].spec(hit[2]) if hit else None
        fallback = CONTEXT_WINDOW if name == "context_window" else MAX_OUTPUT
        setattr(self, name, (spec.context if name == "context_window" else spec.max_output)
                if spec else fallback)

    def switch_provider(self, client, model: str, *, provider_name: str,
                        reasoning_policy: str, tools_enabled: bool = True,
                        vision: bool = False, local: bool = False,
                        provider=None, endpoint=None, route=None) -> None:
        """Point the engine at a different OpenAI-compatible endpoint/model live
        (a /model switch). The caller builds the client from a route
        (engine/route.py) and passes it along, so an auth error can still say
        where the key came from and where it went; we swap model + reasoning
        shape. With the registry
        `provider`/`endpoint` given, everything else (effort tiers, cache
        field, un-pinned limits) is adopted from the model's spec too; the
        explicit flags stay as the override for callers without a registry
        entry. The prompt-cache prefix is provider-specific, so the switch
        naturally starts a fresh cache — no stale-hit risk. History and tools
        are untouched (same OpenAI protocol)."""
        self.client = client
        self.route = route  # None = the caller owns the transport
        self._instrumented = self._instrument(client)
        if provider is not None and endpoint is not None:
            self.adopt(provider, endpoint, model)
            self.provider_name = provider_name or endpoint.eid
            self.tools_enabled = tools_enabled and self.tools_enabled
            self.vision_enabled = vision or self.vision_enabled  # a preflight may add sight
        else:
            self.model = model
            self.wire_model = model
            self.protocol = GENERIC
            self.model_max_output = MAX_OUTPUT
            self.provider_name = provider_name
            self.reasoning_policy = "thinking" if reasoning_policy == "deepseek" else reasoning_policy
            self.efforts = None
            self.thinking_off = True
            self.cache_field = ("prompt_cache_hit_tokens" if self.reasoning_policy == "thinking"
                                and provider_name.startswith("deepseek") else "cached_tokens")
            self.tools_enabled = tools_enabled
            self.vision_enabled = vision
            self.provider_local = local
        self._mark_cache_reset("model switch")
        self.trajectory.note({"provider": self.provider_name, "model": self.model,
                              "vision": self.vision_enabled, "local": self.provider_local,
                              "context_window": self.context_window,
                              "max_tokens": self.max_tokens})

    def _repair_history(self) -> None:
        """Inject synthetic tool responses for ANY orphaned tool_calls.

        A tool_calls assistant message with no matching tool response for one of
        its ids makes the API 400. This happens after a hard kill mid-tool, or
        when a turn is cancelled (new submit / Esc). This is:
          - idempotent: a call that already has a response is left alone, so it
            is safe to call repeatedly (the turn's finally + the next turn's
            pre-flight both call it) without ever producing a DUPLICATE response;
          - in-position: a stub goes right after its own assistant message
            (past any responses already there), never at the end — so it can't
            land after a newly-appended user message and break ordering.
        Fixes every orphan, not just the most recent (a multi-orphan resume
        otherwise still 400s).

        Shared with `session.repair_history` — the --resume path runs the same
        fix on trajectory-reloaded history (a hard-killed process never reaches
        this finally-block, so the load path must self-heal too).
        """
        repair_history(self.history)

    def _append(self, msg: dict) -> None:
        self.history.append(msg)
        self.trajectory.message(msg)

    _TODAY_RE = re.compile(r"Today is \d{4}-\d{2}-\d{2} \(\w+\)\.")

    def _mark_cache_reset(self, reason: str) -> None:
        """Rocky is forfeiting the prompt-cache prefix anyway (model switch /
        compaction / mode change). Record why — the eviction detector mutes
        itself for the next request — and use the FREE window to re-stamp
        prefix facts that drift in long sessions. Today that is only the
        `with_today` date line (an overnight session otherwise reasons from
        yesterday); a same-day re-stamp is byte-identical, i.e. a no-op, so
        this never costs cache outside a real calendar flip."""
        self._cache_reset_reason = reason
        from datetime import datetime
        now = datetime.now()
        stamp = f"Today is {now:%Y-%m-%d} ({now:%A})."
        base = self._TODAY_RE.sub(stamp, self._base_system)
        if base != self._base_system:
            self._base_system = base
            self.history[0]["content"] = self._TODAY_RE.sub(
                stamp, self.history[0]["content"])
            self.trajectory.note({"prefix_refresh": {"today": f"{now:%Y-%m-%d}",
                                                     "at": reason}})

    def _observe_cache(self, u: dict) -> Optional[CacheReset]:
        """Per-request cache accounting from the API's OWN numbers (never an
        estimate). With an append-only history, this request's hit tokens
        should be ≈ the previous request's whole prompt (floored to the
        provider's cache block). A hit far below that with no rocky-caused
        reset means the provider evicted the prefix — DeepSeek publishes no
        TTL ("hours to days"), so these trajectory notes, with their idle
        gaps, are the only way to learn the real rule empirically. Known
        resets are logged too, just without the user-facing event. `u` has
        been normalized (run_turn) so the hit count is always under
        prompt_cache_hit_tokens whatever field the provider used."""
        reason, self._cache_reset_reason = self._cache_reset_reason, None
        if "prompt_cache_hit_tokens" not in u:
            return None  # this provider doesn't report cache — nothing to observe
        prompt = int(u.get("prompt_tokens") or 0)
        hit = int(u.get("prompt_cache_hit_tokens") or 0)
        now = time.monotonic()
        prev, prev_at = self._cache_prev, self._cache_req_at
        self._cache_prev, self._cache_req_at = (prompt, hit), now
        if prompt <= 0 or prev is None:
            return None
        expected = (prev[0] // CACHE_BLOCK) * CACHE_BLOCK
        idle = round(now - prev_at, 1) if prev_at is not None else 0.0
        # 2048-token floor: a tiny session losing its cache costs pennies and
        # would only produce alarm noise.
        dropped = reason is None and expected >= 2048 and hit < expected // 2
        if dropped or reason is not None:
            self.trajectory.note({"cache": {
                "reason": reason or "evicted", "hit": hit,
                "expected": expected, "prompt": prompt, "idle_s": idle}})
        if dropped:
            return CacheReset(hit_tokens=hit, expected_tokens=expected, idle_s=idle)
        return None

    def _attach_rewrite(self, name: str, output: str, pending: list[str]) -> str:
        """Consume view_image's direct-vision marker (tools.VIEW_ATTACH_PREFIX):
        the tool response becomes a pointer, the path queues for _attach_images.
        Runs BEFORE the output is appended/yielded, so the marker never reaches
        history, the trajectory, or the screen."""
        if name == "view_image" and output.startswith(tools_mod.VIEW_ATTACH_PREFIX):
            path = output[len(tools_mod.VIEW_ATTACH_PREFIX):].strip()
            pending.append(path)
            return (f'[image "{Path(path).name}" attached — it is shown to you '
                    "in the user message that follows]")
        return output

    def _attach_images(self, paths: list[str]) -> None:
        """The images view_image fetched this batch, as ONE user message right
        after the batch's tool responses — providers accept images on user
        messages only, so a tool response can't carry them itself. Internal
        image_path parts, same as a paste: api_view inflates them for a vision
        model and collapses them to text if the session later switches away."""
        if not paths:
            return
        names = ", ".join(Path(p).name for p in paths)
        self._append({"role": "user", "content": images_mod.make_content(
            f"[image attached by view_image: {names}]",
            [images_mod.image_part(p) for p in paths])})

    def _is_read(self, name: str) -> bool:
        """A read-like tool — 'safe' risk tier, i.e. non-mutating (read_file /
        grep / glob / check_code / recall_memory). Read calls can run concurrently
        with one another; anything that writes or has side effects (edit / write /
        bash / web / mcp) stays serial so two writes can never clobber."""
        return getattr(self.registry.get(name), "risk", "risky") == "safe"

    async def _approve(self, name: str, arguments_json: str):
        """Ask the injected approver whether this tool call may run: True, False,
        or a Refusal (falsy) whose text becomes the tool's result. Parses args
        to a dict for the approver; a malformed payload becomes {} (execute will
        surface the real error). Short-circuits the always-allow default so
        headless/bench runs pay nothing."""
        if self.approver is _always_allow:
            return True
        try:
            args = json.loads(arguments_json) if arguments_json.strip() else {}
        except (json.JSONDecodeError, AttributeError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        answer = await self.approver(name, args)
        return answer if isinstance(answer, Refusal) else bool(answer)

    @staticmethod
    def _refused(answer) -> str:
        """The tool result for a call the approver refused: its own words, else the stock line."""
        return str(answer) if isinstance(answer, Refusal) else "[denied] user rejected this tool call"

    def _turn_stopped(self) -> bool:
        """The approver ended this turn (the park limit, engine/approval.py). The rest
        of the batch is answered without running, then the turn finishes as any other."""
        return bool(getattr(self.approver, "stop_turn", False))

    def _plan_gate(self, name: str, arguments_json: str) -> planmode.Verdict:
        """Plan-mode read-only gate, evaluated BEFORE the approver. Always
        'pass' when plan mode is off, so normal sessions pay nothing."""
        if self.plan_file is None:
            return planmode.PASS
        try:
            args = json.loads(arguments_json) if arguments_json.strip() else {}
        except (json.JSONDecodeError, AttributeError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        tool = self.registry.get(name)
        root = getattr(tool, "workspace_root", None)
        if root is not None and isinstance(args.get("path"), str):
            try:
                args["path"] = str(self.workdir / Path(args["path"]).relative_to(root))
            except ValueError:
                if Path(args["path"]).is_absolute():
                    return planmode.Verdict("deny", "[blocked] plan mode: use a project-relative "
                                             f"plan path or a path under {root}")
        risk = getattr(tool, "risk", "risky")
        return planmode.gate(name, args, risk, self.plan_file, self.workdir)

    def _projected_prompt_tokens(self) -> int:
        """Best guess at the next call's prompt size: the API's real count
        for everything already sent, plus a conservative estimate for what
        was appended since."""
        return self._last_prompt_tokens + compaction.estimate_tokens(
            self.history[self._sent_until :]
        )

    async def _maybe_compact(self, usage_total: dict[str, int]) -> AsyncIterator[Event]:
        """Shrink history if the next call would crowd the context window.

        Free deterministic prune first; an LLM state summary only when
        pruning can't get under the limit. Never raises — a failed summary
        degrades to the prune and the turn continues.
        """
        projected = self._projected_prompt_tokens()
        # Soft, non-blocking nudge once we cross the degrade mark (~50%); re-arms
        # after we drop back under it (a compaction, or a /clear).
        if projected <= int(self.context_window * self.remind_threshold):
            self._reminded = False
        elif not self._reminded:
            self._reminded = True
            yield ContextReminder(pct=projected / self.context_window, window=self.context_window)

        limit = int(self.context_window * self.compact_threshold)
        if projected <= limit or len(self.history) < 3:
            return
        yield StateChanged(state=AgentState.COMPACTING)
        # Reflect over the pre-compaction view (snapshotted inside) — the
        # rewrite below is about to squash exactly the detail a checkpoint
        # wants, and this prefix is the one the provider has cached right now.
        self._boundary(reason="compaction")

        before_msgs = len(self.history)
        record: dict = {}
        # Cap any single oversized message FIRST — a huge paste or tool result the
        # tail keeps verbatim would otherwise survive prune (tool-only) and
        # summarize (keeps the tail), keeping every step over the limit, or would
        # overflow the summarize call itself. Head+tail truncation bounds it.
        truncated = compaction.truncate_oversized(self.history)
        if truncated:
            record["truncated_oversized"] = truncated
            projected = compaction.estimate_tokens(self.history)
        tail = compaction.tail_start(
            self.history,
            min(compaction.KEEP_RECENT_TOKENS, compaction.estimate_tokens(self.history) // 2),
        )
        record["tail_start"] = tail

        if projected - compaction.prune_savings(self.history, tail) <= limit:
            strategy = "prune"
            record["pruned_tool_outputs"] = compaction.prune_tool_outputs(self.history, tail)
        else:
            strategy = "summarize"
            try:
                summary, usage = await compaction.summarize(
                    self.client, self.wire_model,
                    # Same view as the main call — the request prefix matches
                    # what the provider has cached, and image_path parts never
                    # reach the wire. LOCAL providers get no tool schemas:
                    # their compat layers drop tool_choice="none", and a local
                    # model handed tools may emit a call instead of a summary.
                    self._api_messages(tools=self.tools_enabled and not self.provider_local
                                       and bool(self.registry)),
                    tools=([] if self.provider_local or not self.tools_enabled
                           else [t.schema for t in self.registry.values()]),
                    reasoning=self.reasoning_policy,
                    efforts=self.efforts, thinking_off=self.thinking_off,
                    protocol=self.protocol,
                )
                if not summary:
                    raise ValueError("model returned an empty summary")
            except Exception as e:  # noqa: BLE001 — degrade to prune, never kill the turn
                strategy = "prune"
                record["pruned_tool_outputs"] = compaction.prune_tool_outputs(self.history, tail)
                yield EngineError(
                    message=f"compaction summary failed ({type(e).__name__}: {e}) — "
                    "pruned old tool outputs instead"
                )
            else:
                _merge_usage(usage_total, usage)
                _merge_usage(self.stats.usage, usage)
                self.history = [self.history[0], compaction.state_message(summary)] + self.history[tail:]
                record["summary"] = summary
                record["new_history"] = list(self.history)

        self._last_prompt_tokens = 0
        self._sent_until = 0
        self._mark_cache_reset("compaction")  # prefix forfeited → free refresh window
        self.stats.compactions += 1
        tokens_after = compaction.estimate_tokens(self.history)
        # Resume the compacted history, including reasoning in the retained tail.
        record["new_history"] = copy.deepcopy(self.history)
        self.trajectory.compaction(
            {
                "strategy": strategy,
                "tokens_before": projected,
                "tokens_after_est": tokens_after,
                "messages_before": before_msgs,
                "messages_after": len(self.history),
                **record,
            }
        )
        yield Compacted(
            strategy=strategy,
            tokens_before=projected,
            tokens_after=tokens_after,
            messages_before=before_msgs,
            messages_after=len(self.history),
        )

    async def run_turn(
        self, user_message: str, *,
        images: Optional[list] = None,
    ) -> AsyncIterator[Event]:
        """One full user turn: stream → maybe tools → stream → … → answer.

        `images`: what the user attached — paths (str/Path), or ready-made
        image_path part dicts when a vision route already annotated them with
        a description (the TUI's no-vision flow). Stored in history as small
        reference parts; the base64 only ever exists in the API request
        (images.api_view).
        """
        self._pending_turn_state = None
        self._turn_id = uuid.uuid4().hex[:12]
        begin = getattr(self.approver, "begin_turn", None)
        if callable(begin):
            begin()  # the approver counts some things per turn (the park limit)
        yield TurnStarted(user_message=user_message, turn=self._turn_id)  # UI sees the clean message
        self.stats.turns += 1
        self.planning.continue_discussion()
        if self.plan_file is not None:
            self.planning.sync_prompt()
            user_message = "[Plan mode is active; discuss and plan only.]\n\n" + user_message
        parts = [p if isinstance(p, dict) else images_mod.image_part(p)
                 for p in (images or [])]
        self._append({"role": "user", "content": images_mod.make_content(user_message, parts)})

        async with aclosing(self._run_loop()) as stream:
            async for ev in stream:
                yield ev

    def export_turn_state(self) -> Optional[dict]:
        """Checkpoint loop position while the client is about to admit a request.

        Callers must also persist history/workspace and reconcile request usage.
        This is not a license to replay an already-issued request. None means
        no exportable request boundary (e.g. tools are executing or turn ended).
        """
        if self._pending_turn_state is None:
            return None
        return {**copy.deepcopy(self._pending_turn_state), 'version': 1,
                'max_steps': self.max_steps, 'finalize_steps': self.finalize_steps,
                'model': self.model, 'history_sha256': self._history_digest()}

    def _history_digest(self) -> str:
        return hashlib.sha256(json.dumps(self.history, sort_keys=True,
                                        ensure_ascii=False).encode()).hexdigest()

    async def resume_turn(self, *, state: Optional[dict] = None) -> AsyncIterator[Event]:
        """Continue a clean request boundary without resetting a finite budget.

        Finite turns require export_turn_state() from the saved admission point.
        History, workspace and known usage must be restored together by caller.
        Legacy unbounded checkpoints remain supported without loop state.
        """
        if self.max_steps > 0 and state is None:
            raise ValueError("finite resume requires a saved turn state; cannot reset the step cap")
        if not self.history or self.history[-1]["role"] not in ("user", "tool"):
            raise ValueError("resume_turn needs an unfinished turn at a request boundary")
        repaired = copy.deepcopy(self.history)
        repair_history(repaired)
        if repaired != self.history:
            raise ValueError("resume_turn cannot replay unresolved tool calls")
        if state is not None:
            if (state.get('version') != 1 or state.get('max_steps') != self.max_steps
                    or state.get('finalize_steps') != self.finalize_steps
                    or state.get('model') != self.model
                    or state.get('history_sha256') != self._history_digest()):
                raise ValueError("turn state history/model/budget mismatch")
            step = state.get('step')
            if (type(step) is not int or step < 1 or
                    (self.max_steps > 0 and step > self.max_steps + self.finalize_steps)
                    or state.get('phase') not in ('compact', 'request')
                    or type(state.get('used_tools')) is not bool
                    or not isinstance(state.get('usage_total'), dict)
                    or not isinstance(state.get('turn', ''), str)
                    or any(type(v) is not int or v < 0 for v in state['usage_total'].values())):
                raise ValueError("invalid turn state")
            state = copy.deepcopy(state)
        # A bounded continuation is the same logical turn, so it keeps the id;
        # a legacy unbounded checkpoint saved none and starts a fresh one.
        self._turn_id = (state or {}).get('turn') or uuid.uuid4().hex[:12]
        async with aclosing(self._run_loop(resume_state=state)) as stream:
            async for ev in stream:
                yield ev

    async def _run_loop(self, *, resume_state: Optional[dict] = None) -> AsyncIterator[Event]:
        usage_total: dict[str, int] = dict(resume_state['usage_total']) if resume_state else {}
        used_tools = resume_state['used_tools'] if resume_state else False
        steps = resume_state['step'] - 1 if resume_state else 0

        while True:
            steps += 1
            self.stats.steps += 1

            # max_steps <= 0 means UNLIMITED — no cap, no finalize, no budget
            # warnings. Used by interactive chat so the model runs until it's
            # done; the human (and Esc/new-message interrupt) is the backstop.
            self._pending_turn_state = None
            if self.max_steps > 0 and resume_state is None:
                over = steps - self.max_steps  # >0 ⇒ in the closing window
                if over > self.finalize_steps:
                    self.stats.engine_errors += 1
                    yield EngineError(
                        message=f"step limit ({self.max_steps}+{self.finalize_steps} finalize) "
                        "reached — stopping this turn."
                    )
                    break
                if over >= 1 and not used_tools:
                    # Never used tools; a normal answer would already have returned.
                    self.stats.engine_errors += 1
                    yield EngineError(message=f"step limit ({self.max_steps}) reached — stopping this turn.")
                    break
                if over == 1:
                    notice = ("Planning turn budget reached. Summarize findings and any open "
                              "decisions, then stop. Do not implement or submit an unfinished plan."
                              if self.planning.active
                              else budget_notice(self.finalize_steps, closing=True))
                    self._append({"role": "user", "content": notice})
                elif over <= 0:
                    # One proportional notice at 80%; no arbitrary nudge at step 50.
                    warning_at = max(1, (self.max_steps * 4 + 4) // 5)
                    if steps == warning_at and used_tools and not self.planning.active:
                        remaining = self.max_steps + self.finalize_steps - steps + 1
                        self._append({"role": "user", "content": budget_notice(remaining)})

            self._pending_turn_state = {'step': steps, 'used_tools': used_tools,
                                        'usage_total': usage_total, 'phase': 'compact',
                                        'turn': self._turn_id}
            if resume_state is None or resume_state['phase'] == 'compact':
                async for ev in self._maybe_compact(usage_total):
                    yield ev
            resume_state = None

            self._repair_history()
            sent = len(self.history)
            # Clamp output to what the window can still hold: input can run up to
            # 90% before auto-compaction and max_tokens may be the model's full
            # output cap, so input + output could exceed the window. Reserve the
            # rest for output without exceeding the user or model ceiling.
            out_room = self.context_window - self._projected_prompt_tokens() - 512
            if out_room <= 0:
                yield EngineError(message="No context space is left for a reply. Start a new task to continue.")
                yield StateChanged(state=AgentState.IDLE)
                return
            eff_max_tokens = min(self.max_tokens, self.model_max_output, out_room)
            self._pending_turn_state['phase'] = 'request'
            # tools omitted entirely when the provider profile is tools:off
            # (a model that calls tools badly) — the loop then runs as a
            # plain responder rather than shipping malformed tool calls.
            _tools = [t.schema for t in self.registry.values()] if self.tools_enabled else None
            # One admitted model request: its id, the catalog generation it is
            # built against and the resolved route travel on every receipt its
            # response produces; the transport hook counts the physical sends.
            request_id = uuid.uuid4().hex[:12]
            generation, route, sends = self.registry_generation, self._route(), {"attempts": 0}
            try:
                token = _REQUEST.set(sends)
                try:
                    stream = await self.client.chat.completions.create(
                    model=self.wire_model,
                    # image_path parts inflate to base64 here (or collapse to a
                    # placeholder on a no-vision model) — history itself keeps
                    # only the small reference form.
                    messages=self._api_messages(),
                    tools=_tools,
                    **self.protocol.output_cap(eff_max_tokens),
                    stream=True,
                    stream_options={"include_usage": True},
                    extra_body=self._extra_body(),
                    **self.protocol.sampling(self.thinking),
                )
                finally:
                    _REQUEST.reset(token)
            except Exception as e:  # noqa: BLE001 — surfaced to UI, turn ends
                self.stats.engine_errors += 1
                self._note_request(request_id, steps, sends, generation, route,
                                   tools=_tools, error=type(e).__name__)
                yield StateChanged(state=AgentState.ERROR)
                # Auth-class rejections: say WHICH credential source was used
                # and WHERE it was sent — "which key is rocky even using?"
                # must never need an archaeology session (it once cost an
                # evening). Never the key itself, only its source name.
                # The route this engine built; with an injected client only
                # the home pair is knowable.
                source = (self.route.describe() if self.route is not None else
                          f"{current_key_source() or 'none'} → {require_base_url()}")
                base = self.route.base_url if self.route is not None else require_base_url()
                said = errors_mod.explain(e, model=self.model, host=urlsplit(base).hostname or base, key_source=source)
                if self._host_selected_model:
                    # A host-chosen model: the SDK's text may echo the host's
                    # prompt or an upstream URL, so only its class is named.
                    yield EngineError(message=f"Model request failed for {self.provider_name}:{self.model} "
                                              f"({type(e).__name__})", kind=said.kind, params=said.params)
                else:
                    # One plain sentence with the next step; the SDK's own text rides along.
                    yield EngineError(message=redact(said.message), kind=said.kind, params=said.params,
                                      detail="" if said.kind == "unknown" else redact(f"{type(e).__name__}: {e}")[:600])
                yield StateChanged(state=AgentState.IDLE)
                return

            self._pending_turn_state = None
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_calls: dict[int, dict] = {}
            thinking_seen = False
            content_seen = False

            finish_reason = None
            stream_error = None
            usage_seen = False
            try:
                async for chunk in stream:
                    if chunk.usage is not None:
                        usage_seen = True
                        try:
                            u = chunk.usage.model_dump()
                        except AttributeError:
                            u = dict(chunk.usage)
                        # One cache shape downstream (ledger, trajectory, the
                        # observer): providers report hits under different
                        # fields — the registry says which; normalize here.
                        hit = providers_mod.cache_hit_tokens(u, self.cache_field)
                        if hit is not None and "prompt_cache_hit_tokens" not in u:
                            u["prompt_cache_hit_tokens"] = hit
                        _merge_usage(usage_total, u)
                        _merge_usage(self.stats.usage, u)
                        self.trajectory.usage(u)  # per-call: prompt/completion + cache hit/miss
                        cache_ev = self._observe_cache(u)
                        if cache_ev is not None:
                            yield cache_ev
                        if isinstance(u.get("prompt_tokens"), int):
                            self._last_prompt_tokens = u["prompt_tokens"]
                    if not chunk.choices:
                        continue
                    finish_reason = getattr(chunk.choices[0], "finish_reason", None) or finish_reason
                    delta = chunk.choices[0].delta

                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        if not thinking_seen:
                            thinking_seen = True
                            yield StateChanged(state=AgentState.THINKING)
                        reasoning_parts.append(reasoning)
                        yield ThinkingDelta(text=reasoning)

                    if delta.content:
                        if not content_seen:
                            content_seen = True
                            yield StateChanged(state=AgentState.RESPONDING)
                        content_parts.append(delta.content)
                        yield TextDelta(text=delta.content)

                    for tc in delta.tool_calls or []:
                        acc = tool_calls.setdefault(
                            tc.index, {"id": "", "name": "", "arguments": ""}
                        )
                        if tc.id:
                            acc["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                acc["name"] += tc.function.name
                            if tc.function.arguments:
                                acc["arguments"] += tc.function.arguments

            except Exception as exc:  # a broken stream must never execute partial tools
                stream_error = f"stream interrupted ({type(exc).__name__})"
            finally:
                close = getattr(stream, "close", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:  # cleanup must not mask cancellation or the result
                        pass

            attempts = self._note_request(request_id, steps, sends, generation, route,
                                          tools=_tools, error=stream_error)
            if self._host_selected_model and not usage_seen:
                stream_error = stream_error or "usage unavailable; cannot account for this run"
            problem = stream_error or self.protocol.completion_error(
                finish_reason, tool_calls=bool(tool_calls))
            if problem or self.protocol.strict_finish:
                self.trajectory.note({"completion": {"finish_reason": finish_reason,
                                                    "error": problem,
                                                    "max_tokens": eff_max_tokens}})
            if problem:
                # Streamed text may already be visible, but it is NOT a committed
                # assistant turn. No partial tool batch is added or executed.
                self.stats.engine_errors += 1
                yield StateChanged(state=AgentState.ERROR)
                yield EngineError(message=f"Incomplete model response: {problem}. No tools from this response ran.")
                yield StateChanged(state=AgentState.IDLE)
                return

            self._sent_until = sent
            content = "".join(content_parts)
            reasoning_text = "".join(reasoning_parts)
            # Keep the existing analysis record; the message is authoritative for replay.
            self.trajectory.reasoning(reasoning_text)
            reasoning_fields = ({"reasoning_content": reasoning_text}
                                if self.protocol.replay_reasoning != "never" and
                                (reasoning_text or self.protocol.reasoning_required) else {})

            if not tool_calls:
                # Also preserve no-tool assistant turns: a later request can carry tools.
                self._append({"role": "assistant", "content": content, **reasoning_fields})
                if used_tools:
                    yield StateChanged(state=AgentState.AMAZED)
                # Turn boundary = the idle moment a checkpoint may ride on
                # (background task; the user never waits on it).
                self._boundary()
                yield TurnFinished(steps=steps, usage=usage_total, turn=self._turn_id)
                yield StateChanged(state=AgentState.IDLE)
                return

            used_tools = True
            calls = [tool_calls[i] for i in sorted(tool_calls)]
            self._append(
                {
                    "role": "assistant",
                    "content": content or None,
                    **reasoning_fields,
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": c["arguments"]},
                        }
                        for c in calls
                    ],
                }
            )

            envelopes = [Envelope(session=self.trajectory.session_id, turn=self._turn_id,
                                  request=request_id, attempt=attempts,
                                  logical_call_id=f"{request_id}.{i}", provider_call_id=c["id"],
                                  registry_generation=generation, route=route)
                         for i, c in enumerate(calls, 1)]
            answered: set[str] = set()
            dispatched: set[str] = set()  # handed to a tool — a cancel past this point is "unknown"
            # A read-only batch (every call is a non-mutating 'safe'-tier tool) runs
            # CONCURRENTLY — reads can't conflict, and reading/grepping several files
            # at once is the common explore step. Any batch containing a write / side
            # effect stays SERIAL so two writes never clobber. Either way, responses
            # are appended in the original tool_calls ORDER, which the API requires.
            parallel = len(calls) > 1 and all(self._is_read(c["name"]) for c in calls)
            try:
                yield StateChanged(state=AgentState.TOOL)
                if parallel:
                    for c, env in zip(calls, envelopes):
                        yield ToolStarted(call_id=c["id"], tool=c["name"], args={"raw": c["arguments"]},
                                          envelope=env)
                    # Approvals stay sequential (a modal can't run N-at-once); an
                    # in-workdir read auto-allows instantly, so this is usually a no-op.
                    allowed = {c["id"]: await self._approve(c["name"], c["arguments"]) for c in calls}
                    runnable = [(c, env) for c, env in zip(calls, envelopes) if allowed[c["id"]]]
                    t0 = time.monotonic()
                    dispatched.update(c["id"] for c, _ in runnable)
                    results = await asyncio.gather(*(
                        self._execute(env, c["name"], c["arguments"]) for c, env in runnable
                    ))
                    dt = time.monotonic() - t0
                    out_by_id = {c["id"]: r for (c, _), r in zip(runnable, results)}
                    attach: list[str] = []
                    finished = []
                    for c, env in zip(calls, envelopes):  # original order — required by the API
                        if c["id"] in out_by_id:
                            output, ok = out_by_id[c["id"]]
                            output = self._attach_rewrite(c["name"], output, attach)
                            self.stats.observe_tool(c["name"], c["arguments"], output, ok)
                            receipt = "ok" if ok else "error"
                        else:
                            output, ok = self._refused(allowed[c["id"]]), False
                            self.stats.denials += not isinstance(allowed[c["id"]], Refusal)  # a user's "no" only
                            receipt = "refused"
                        self._append({"role": "tool", "tool_call_id": c["id"], "content": output})
                        answered.add(c["id"])
                        self._receipt(env, c["name"], receipt, dt)
                        finished.append(ToolFinished(call_id=c["id"], tool=c["name"],
                                                     output=output, ok=ok, duration_s=dt, envelope=env))
                    self._attach_images(attach)
                    # All gathered tools already finished. Persist every result
                    # before a consumer can close the generator at the first event.
                    for ev in finished:
                        yield ev
                else:
                    attach = []
                    for c, env in zip(calls, envelopes):
                        yield ToolStarted(call_id=c["id"], tool=c["name"], args={"raw": c["arguments"]},
                                          envelope=env)
                        t0 = time.monotonic()
                        # Plan mode (if on) gates BEFORE the approver: a write to
                        # the plan file runs, other mutations are denied with a
                        # teaching message, read-only calls fall through to ask.
                        verdict = self._plan_gate(c["name"], c["arguments"])
                        answer = True
                        if self._turn_stopped():
                            verdict, answer = planmode.PASS, Refusal(_TURN_STOPPED)
                        elif verdict.action == "pass":
                            answer = await self._approve(c["name"], c["arguments"])
                        if verdict.action == "deny":
                            output, ok = verdict.message, False
                            self.stats.plan_denials += 1
                            receipt = "refused"
                        elif answer:
                            dispatched.add(c["id"])
                            output, ok = await self._execute(env, c["name"], c["arguments"])
                            output = self._attach_rewrite(c["name"], output, attach)
                            self.stats.observe_tool(c["name"], c["arguments"], output, ok)
                            receipt = "ok" if ok else "error"
                        else:
                            output, ok = self._refused(answer), False
                            self.stats.denials += not isinstance(answer, Refusal)  # a user's "no" only
                            receipt = "refused"
                        # Consumers (notably headless approval stops) can close
                        # us at this yield. Record the known result first, so it
                        # cannot become an ambiguous "interrupted" stub on resume.
                        self._append({"role": "tool", "tool_call_id": c["id"], "content": output})
                        answered.add(c["id"])
                        dt = time.monotonic() - t0
                        self._receipt(env, c["name"], receipt, dt)
                        yield ToolFinished(
                            call_id=c["id"],
                            tool=c["name"],
                            output=output,
                            ok=ok,
                            duration_s=dt,
                            envelope=env,
                        )
                    self._attach_images(attach)
            finally:
                # Invariant: every tool_call_id above MUST get a matching tool response
                # or the next request (and --resume) 400s. On interrupt (a new submit or
                # Esc cancels the worker → CancelledError at an await) some calls never
                # ran. Two parts:
                #   1) in-memory history — _repair_history backfills IN-POSITION and
                #      IDEMPOTENTLY. Critical: a concurrent new turn may already have
                #      appended its user message + run its own repair, so the OLD
                #      end-append produced a duplicate + misordered tool message that
                #      bricked the session with a permanent 400. Reusing repair can't
                #      double-insert, and it's sync (no await) so it finishes before
                #      control returns to the new turn.
                #   2) the append-only trajectory — log a stub for each un-run call so a
                #      --resume reload also has the response. Best-effort; a failing
                #      trajectory write must never mask the cancellation.
                self._repair_history()
                if any(c["id"] not in answered for c in calls):
                    # Esc / a new submit landed mid-batch — a strong "the user
                    # wanted something else" signal for the outcome record.
                    self.stats.interrupts += 1
                for c, env in zip(calls, envelopes):
                    if c["id"] not in answered:
                        try:
                            self.trajectory.message({
                                "role": "tool",
                                "tool_call_id": c["id"],
                                "content": "[error] tool execution was interrupted",
                            })
                            self._receipt(env, c["name"],
                                          "unknown" if c["id"] in dispatched else "cancelled", 0.0)
                        except Exception:  # noqa: BLE001 — never mask the cancel with I/O
                            pass
            if self._turn_stopped():
                break  # every call of the batch has its answer: finish the turn below, usage and all

        self._boundary()  # step-limit exit is a turn boundary too
        yield TurnFinished(steps=steps, usage=usage_total, turn=self._turn_id)
        yield StateChanged(state=AgentState.IDLE)
