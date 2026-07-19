"""The agent loop: stream DeepSeek with native tool calls, execute tools,
repeat until the model answers without tools. Emits Events; owns history.

Before every API call the loop projects the next prompt size (last real
prompt_tokens + char-based estimates for newer messages) and, over the
threshold, compacts history via compaction.py (prune → state summary).

DeepSeek specifics handled here (see memory: reference_deepseek_api):
- thinking + reasoning_effort go in extra_body
- the stream carries delta.reasoning_content AND delta.content
- reasoning_content must NOT be sent back in history (HTTP 400) — we only
  ever append {role, content, tool_calls} for assistant turns
- usage extras (cache hit/miss) are read via model_dump()
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Optional

from openai import AsyncOpenAI

from rockycode.onboarding import current_key_source, require_base_url, require_key

from rockycode.engine import compaction
from rockycode.engine import planmode
from rockycode.engine import tools as tools_mod
from rockycode.engine.effort import build_extra_body
from rockycode.engine.redact import redact
from rockycode.engine.events import (
    AgentState,
    Compacted,
    ContextReminder,
    EngineError,
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

MAX_STEPS = 50
FINALIZE_STEPS = 3  # forced wrap-up window after the explore budget is spent
CONTEXT_WINDOW = 1_048_576  # DeepSeek V4's real 1M window (matches the bench config)
# Two-tier context handling. DeepSeek V4 degrades past ~50%, but forcing a
# compaction there interrupts the user — so 50% is a SOFT one-time reminder
# (ContextReminder), and automatic compaction only fires near the ceiling.
REMIND_THRESHOLD = 0.50   # non-blocking nudge: "past the half, /clear if you want"
COMPACT_THRESHOLD = 0.90  # hard auto-compact — the safety net before overflow

# Soft nudge while there's still explore budget. Evidence (dev10, twice):
# without pressure the model explores right through the cap and submits
# nothing — matplotlib-23476 burned all 50 steps on investigation, zero edits.
BUDGET_WARNINGS = {
    10: (
        "[harness] only 10 steps remain. stop exploring — commit to your best "
        "fix now, make the edit, verify with one targeted test run, then finish."
    ),
}

# The finalize loop: once explore budget is spent, the model gets FINALIZE_STEPS
# extra turns with this hard instruction so it ALWAYS applies its fix instead of
# stopping empty-handed. This is the "make sure it finishes" mechanism.
FINALIZE_NOTICE = (
    "[harness] EXPLORE BUDGET SPENT — this is your final chance. If your fix is "
    "not yet written to disk, apply it NOW with edit_file/write_file, then stop. "
    "Do not read or search anymore. When the edit is saved, reply DONE with a one-"
    "line summary of what you changed."
)


def _merge_usage(total: dict[str, int], usage: dict) -> None:
    for k, v in usage.items():
        if isinstance(v, int):
            total[k] = total.get(k, 0) + v


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
        max_tokens: int = 384_000,  # DeepSeek V4 max output (384K); never truncate
        workdir: Optional[Path] = None,
        allowed_roots: tuple[Path, ...] = (),
        system_prompt: str = ROCKY_SYSTEM,
        client: Optional[AsyncOpenAI] = None,
        registry: Optional[dict[str, tools_mod.Tool]] = None,
        trajectory_meta: Optional[dict] = None,
        context_window: int = CONTEXT_WINDOW,
        compact_threshold: float = COMPACT_THRESHOLD,
        remind_threshold: float = REMIND_THRESHOLD,
        max_steps: int = MAX_STEPS,
        finalize_steps: int = FINALIZE_STEPS,
        approver: Optional[Callable[[str, dict], Awaitable[bool]]] = None,
    ) -> None:
        self.model = model
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        # Which reasoning-param shape the active provider wants (deepseek |
        # openai | none). Set by a /model switch; defaults to DeepSeek, rocky's
        # home provider, so existing behavior is byte-identical.
        self.reasoning_policy = "deepseek"
        self.provider_name = "deepseek"
        self.tools_enabled = True  # profile tools:off → drop tool schemas
        self.max_tokens = max_tokens
        self.context_window = context_window
        self.compact_threshold = compact_threshold
        self.remind_threshold = remind_threshold
        self._reminded = False  # fired the soft 50% nudge? re-arms below the mark
        self.max_steps = max_steps
        self.finalize_steps = finalize_steps
        # Opt-in approval gate. Default = always-allow, so bench/headless and the
        # current TUI stay unchanged until a real approver is injected (the TUI
        # sets one that runs permission.decide() + a modal). Used by _approve.
        self.approver = approver or _always_allow
        # Plan mode: set to the session's plan-file path to make this engine
        # read-only — planmode.gate() then runs on every tool call BEFORE the
        # approver (the plan file itself is the one writable target). None = off.
        # Host state only; the model never toggles it (docs/plan-mode-design.md).
        self.plan_file: Optional[Path] = None
        # Resolved paths a live human approval widened the READ jail to (read_file
        # only). Mutable + held by reference in the registry, so an approval takes
        # effect immediately. Never fed by a file — only a launch flag or a click.
        self.read_grants: set[Path] = set()
        # Compaction bookkeeping: the API's real prompt size for everything
        # up to history[_sent_until], estimates for anything appended after.
        self._last_prompt_tokens = 0
        self._sent_until = 0
        # Heuristic outcome counters (self-evolve phase 0), incremented at the
        # exact branch points below and flushed as ONE `outcome` record by
        # finalize_outcome() when the session ends.
        self.stats = SessionStats()
        self._outcome_written = False
        self.workdir = workdir or Path.cwd()
        self.allowed_roots = allowed_roots
        # Explicit key AND endpoint (not the SDK's env fallbacks): an ambient
        # OPENAI_API_KEY/OPENAI_BASE_URL must never decide what gets sent where.
        self.client = client or AsyncOpenAI(api_key=require_key(), base_url=require_base_url(),
                                            max_retries=5, timeout=300.0)
        self.registry = (
            registry if registry is not None
            else tools_mod.build_registry(self.workdir, allowed_roots, read_grants=self.read_grants)
        )
        self.history: list[dict] = [{"role": "system", "content": system_prompt}]
        # Collaboration mode (host-owned, like plan_file — the model never
        # toggles it): set_mode() swaps a contract into the system prompt.
        self._base_system = system_prompt
        self._mode_contract: Optional[str] = None
        self.mode_name: Optional[str] = None
        self.resumed_mode: Optional[str] = None  # mode seen in a resumed session's prompt
        self.trajectory = TrajectoryLogger(
            meta={
                "model": model,
                "thinking": thinking,
                "reasoning_effort": reasoning_effort,
                "max_tokens": max_tokens,
                "context_window": context_window,
                "max_steps": max_steps,
                "workdir": str(self.workdir),
                "base_url": require_base_url(),
                **(trajectory_meta or {}),
            }
        )
        self.trajectory.message(self.history[0])

    def resume(self, messages: list[dict], *, from_session: Optional[str] = None) -> list[dict]:
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
        carried = [m for m in messages if m.get("role") != "system"]
        for m in carried:
            self._append(m)
        self.trajectory.note({"resumed_from": from_session, "carried": len(carried)})
        return carried

    def swap_registry(self, registry: dict[str, tools_mod.Tool]) -> None:
        """Hot-swap the tool registry at runtime (e.g. sandbox on/off)."""
        self.registry = registry

    def finalize_outcome(self) -> Optional[dict]:
        """Write the session's heuristic outcome record (self-evolve phase 0).

        Called once at session end — cli: after the app closes, before the
        exit card. Idempotent, and skipped when no user turn ever ran, so an
        open-and-close session doesn't grow a meaningless reward line. The
        judge-graded outcome (source="judge") is appended much later, by dream.
        """
        if self._outcome_written or self.stats.turns == 0:
            return None
        self._outcome_written = True
        data = {"source": "heuristic", **self.stats.as_data()}
        self.trajectory.outcome(data)
        return data

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

    def set_mode(self, name: str, contract: str) -> None:
        """Swap a collaboration contract into the system prompt (a REAL swap,
        not an appended note — models weight the system prompt hardest). Costs
        one prefix-cache miss at the switch, which the user accepted; switching
        at launch (config `mode`) costs nothing. One mode at a time: setting a
        new one replaces the old."""
        self.history[0]["content"] = (
            f"{self._base_system}\n\n# Collaboration mode: {name}\n\n{contract.strip()}"
        )
        self.mode_name = name
        self._mode_contract = contract
        self.trajectory.note({"mode": name})

    def clear_mode(self) -> None:
        """Back to the plain rocky contract."""
        if self.mode_name is None:
            return
        self.history[0]["content"] = self._base_system
        self.mode_name = None
        self._mode_contract = None
        self.trajectory.note({"mode": None})

    def _extra_body(self) -> dict:
        # self.reasoning_effort holds the dial value (high|xhigh|max) and is
        # mutable mid-session (/effort); the provider clamp + param shape happen
        # per call, keyed by the active provider's reasoning policy.
        return build_extra_body(self.thinking, self.reasoning_effort, self.reasoning_policy)

    def switch_provider(self, client, model: str, *, provider_name: str,
                        reasoning_policy: str, tools_enabled: bool = True) -> None:
        """Point the engine at a different OpenAI-compatible endpoint/model live
        (a /model switch). The caller builds the client with the provider's
        base_url + key; we swap model + reasoning policy. The prompt-cache prefix
        is provider-specific, so the switch naturally starts a fresh cache — no
        stale-hit risk. History and tools are untouched (same OpenAI protocol)."""
        self.client = client
        self.model = model
        self.provider_name = provider_name
        self.reasoning_policy = reasoning_policy
        self.tools_enabled = tools_enabled
        self.trajectory.note({"provider": provider_name, "model": model})

    def _repair_history(self) -> None:
        """Inject synthetic tool responses for ANY orphaned tool_calls.

        A tool_calls assistant message with no matching tool response for one of
        its ids makes DeepSeek 400. This happens after a hard kill mid-tool, or
        when a turn is cancelled (new submit / Esc). This is:
          - idempotent: a call that already has a response is left alone, so it
            is safe to call repeatedly (the turn's finally + the next turn's
            pre-flight both call it) without ever producing a DUPLICATE response;
          - in-position: a stub goes right after its own assistant message
            (past any responses already there), never at the end — so it can't
            land after a newly-appended user message and break ordering.
        Fixes every orphan, not just the most recent (a multi-orphan resume
        otherwise still 400s).
        """
        i = 0
        while i < len(self.history):
            msg = self.history[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                answered = {
                    self.history[j].get("tool_call_id")
                    for j in range(i + 1, len(self.history))
                    if self.history[j].get("role") == "tool"
                }
                insert_at = i + 1
                while insert_at < len(self.history) and self.history[insert_at].get("role") == "tool":
                    insert_at += 1
                for tc in msg["tool_calls"]:
                    cid = tc.get("id", "")
                    if cid and cid not in answered:
                        self.history.insert(insert_at, {
                            "role": "tool",
                            "tool_call_id": cid,
                            "content": "[error] tool execution was interrupted",
                        })
                        insert_at += 1
            i += 1

    def _append(self, msg: dict) -> None:
        self.history.append(msg)
        self.trajectory.message(msg)

    def _is_read(self, name: str) -> bool:
        """A read-like tool — 'safe' risk tier, i.e. non-mutating (read_file /
        grep / glob / check_code / recall_memory). Read calls can run concurrently
        with one another; anything that writes or has side effects (edit / write /
        bash / web / mcp) stays serial so two writes can never clobber."""
        return getattr(self.registry.get(name), "risk", "risky") == "safe"

    async def _approve(self, name: str, arguments_json: str) -> bool:
        """Ask the injected approver whether this tool call may run. Parses args
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
        return bool(await self.approver(name, args))

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
        risk = getattr(self.registry.get(name), "risk", "risky")
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
                    self.client, self.model, self.history,
                    tools=[t.schema for t in self.registry.values()],
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
        self.stats.compactions += 1
        tokens_after = compaction.estimate_tokens(self.history)
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

    async def run_turn(self, user_message: str) -> AsyncIterator[Event]:
        """One full user turn: stream → maybe tools → stream → … → answer."""
        yield TurnStarted(user_message=user_message)  # UI sees the clean message
        self.stats.turns += 1
        # Plan mode rides the USER turn (never the system prompt/tools → the cached
        # prompt prefix stays byte-identical when the mode toggles).
        if self.plan_file is not None:
            user_message = planmode.marker(self.plan_file) + "\n\n" + user_message
        self._append({"role": "user", "content": user_message})

        usage_total: dict[str, int] = {}
        used_tools = False
        steps = 0

        while True:
            steps += 1
            self.stats.steps += 1

            # max_steps <= 0 means UNLIMITED — no cap, no finalize, no budget
            # warnings. Used by interactive chat so the model runs until it's
            # done; the human (and Esc/new-message interrupt) is the backstop.
            if self.max_steps > 0:
                over = steps - self.max_steps  # >0 ⇒ in the forced finalize window
                if over > self.finalize_steps:
                    self.stats.engine_errors += 1
                    yield EngineError(
                        message=f"step limit ({self.max_steps}+{self.finalize_steps} finalize) "
                        "reached — stopping this turn."
                    )
                    break
                if over >= 1 and not used_tools:
                    # never acted — no fix to finalize, just stop
                    self.stats.engine_errors += 1
                    yield EngineError(message=f"step limit ({self.max_steps}) reached — stopping this turn.")
                    break
                if over == 1:
                    # entering finalize: one hard instruction to apply the fix now
                    self._append({"role": "user", "content": FINALIZE_NOTICE})
                elif over <= 0:
                    # soft pressure while explore budget remains
                    warning = BUDGET_WARNINGS.get(self.max_steps - steps)
                    if warning is not None and used_tools:
                        self._append({"role": "user", "content": warning})

            async for ev in self._maybe_compact(usage_total):
                yield ev

            self._repair_history()
            sent = len(self.history)
            # Clamp output to what the window can still hold: input can run up to
            # 90% before auto-compaction and max_tokens may be the full 384K, so
            # input + output could exceed the window. Reserve the rest for output,
            # never below a usable floor.
            out_room = self.context_window - self._projected_prompt_tokens() - 512
            eff_max_tokens = max(1024, min(self.max_tokens, out_room))
            try:
                # tools omitted entirely when the provider profile is tools:off
                # (a model that calls tools badly) — the loop then runs as a
                # plain responder rather than shipping malformed tool calls.
                _tools = [t.schema for t in self.registry.values()] if self.tools_enabled else None
                stream = await self.client.chat.completions.create(
                    model=self.model,
                    messages=self.history,
                    tools=_tools,
                    max_tokens=eff_max_tokens,
                    stream=True,
                    stream_options={"include_usage": True},
                    extra_body=self._extra_body(),
                )
            except Exception as e:  # noqa: BLE001 — surfaced to UI, turn ends
                self.stats.engine_errors += 1
                yield StateChanged(state=AgentState.ERROR)
                # Auth-class rejections: say WHICH credential source was used
                # and WHERE it was sent — "which key is rocky even using?"
                # must never need an archaeology session (it once cost an
                # evening). Never the key itself, only its source name.
                hint = ""
                if getattr(e, "status_code", None) in (400, 401, 402, 403):
                    hint = (f" · key source: {current_key_source() or 'none'}"
                            f" → {require_base_url()}")
                yield EngineError(message=redact(f"{type(e).__name__}: {e}") + hint)
                yield StateChanged(state=AgentState.IDLE)
                return

            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_calls: dict[int, dict] = {}
            thinking_seen = False
            content_seen = False

            async for chunk in stream:
                if chunk.usage is not None:
                    try:
                        u = chunk.usage.model_dump()
                    except AttributeError:
                        u = dict(chunk.usage)
                    _merge_usage(usage_total, u)
                    _merge_usage(self.stats.usage, u)
                    self.trajectory.usage(u)  # per-call: prompt/completion + cache hit/miss
                    if isinstance(u.get("prompt_tokens"), int):
                        self._last_prompt_tokens = u["prompt_tokens"]
                if not chunk.choices:
                    continue
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

            self._sent_until = sent
            content = "".join(content_parts)
            # Trajectory-only (stays OUT of history — see module doc): the
            # thinking trace, for language-adherence checks and RL export.
            self.trajectory.reasoning("".join(reasoning_parts))

            if not tool_calls:
                # Final answer — reasoning_content deliberately not stored.
                self._append({"role": "assistant", "content": content})
                if used_tools:
                    yield StateChanged(state=AgentState.AMAZED)
                yield TurnFinished(steps=steps, usage=usage_total)
                yield StateChanged(state=AgentState.IDLE)
                return

            used_tools = True
            calls = [tool_calls[i] for i in sorted(tool_calls)]
            self._append(
                {
                    "role": "assistant",
                    "content": content or None,
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

            answered: set[str] = set()
            # A read-only batch (every call is a non-mutating 'safe'-tier tool) runs
            # CONCURRENTLY — reads can't conflict, and reading/grepping several files
            # at once is the common explore step. Any batch containing a write / side
            # effect stays SERIAL so two writes never clobber. Either way, responses
            # are appended in the original tool_calls ORDER, which the API requires.
            parallel = len(calls) > 1 and all(self._is_read(c["name"]) for c in calls)
            try:
                yield StateChanged(state=AgentState.TOOL)
                if parallel:
                    for c in calls:
                        yield ToolStarted(call_id=c["id"], tool=c["name"], args={"raw": c["arguments"]})
                    # Approvals stay sequential (a modal can't run N-at-once); an
                    # in-workdir read auto-allows instantly, so this is usually a no-op.
                    allowed = {c["id"]: await self._approve(c["name"], c["arguments"]) for c in calls}
                    runnable = [c for c in calls if allowed[c["id"]]]
                    t0 = time.monotonic()
                    results = await asyncio.gather(*(
                        tools_mod.execute(self.registry, c["name"], c["arguments"]) for c in runnable
                    ))
                    dt = time.monotonic() - t0
                    out_by_id = {c["id"]: r for c, r in zip(runnable, results)}
                    for c in calls:  # original order — required by the API
                        if c["id"] in out_by_id:
                            output, ok = out_by_id[c["id"]]
                            self.stats.observe_tool(c["name"], c["arguments"], output, ok)
                        else:
                            output, ok = "[denied] user rejected this tool call", False
                            self.stats.denials += 1
                        yield ToolFinished(call_id=c["id"], tool=c["name"], output=output, ok=ok, duration_s=dt)
                        self._append({"role": "tool", "tool_call_id": c["id"], "content": output})
                        answered.add(c["id"])
                else:
                    for c in calls:
                        yield ToolStarted(call_id=c["id"], tool=c["name"], args={"raw": c["arguments"]})
                        t0 = time.monotonic()
                        # Plan mode (if on) gates BEFORE the approver: a write to
                        # the plan file runs, other mutations are denied with a
                        # teaching message, read-only calls fall through to ask.
                        verdict = self._plan_gate(c["name"], c["arguments"])
                        if verdict.action == "deny":
                            output, ok = verdict.message, False
                            self.stats.plan_denials += 1
                        elif verdict.action == "allow" or await self._approve(c["name"], c["arguments"]):
                            output, ok = await tools_mod.execute(self.registry, c["name"], c["arguments"])
                            self.stats.observe_tool(c["name"], c["arguments"], output, ok)
                        else:
                            output, ok = "[denied] user rejected this tool call", False
                            self.stats.denials += 1
                        yield ToolFinished(
                            call_id=c["id"],
                            tool=c["name"],
                            output=output,
                            ok=ok,
                            duration_s=time.monotonic() - t0,
                        )
                        self._append({"role": "tool", "tool_call_id": c["id"], "content": output})
                        answered.add(c["id"])
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
                for c in calls:
                    if c["id"] not in answered:
                        try:
                            self.trajectory.message({
                                "role": "tool",
                                "tool_call_id": c["id"],
                                "content": "[error] tool execution was interrupted",
                            })
                        except Exception:  # noqa: BLE001 — never mask the cancel with I/O
                            pass

        yield TurnFinished(steps=steps, usage=usage_total)
        yield StateChanged(state=AgentState.IDLE)
