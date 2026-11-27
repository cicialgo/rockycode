"""Reflect — the evolve loop's capture step (evolve v2, locked 2026-08-02).

One capped call on the SESSION'S OWN model and client, made while the
conversation is still the provider's warm prefix (the request is the live
api_view plus one instruction message, so input is a prompt-cache hit — the
provider only re-sees what it already saw this session). It replaces the old
offline judge + digest + mining trio, which re-sent transcripts to a model
later, colder, and needed a local Ollama nobody had running.

Fires at three boundaries, all engine-side (ReflectHook below, attached to
Engine.hooks — the engine itself knows nothing about reflect):
  - every REFLECT_EVERY (200) history messages, after a turn
  - just before a compaction rewrites history (the detail is about to shrink)
  - once at session end (Engine.run_final_hooks — awaited, small timeout)

Each firing appends ONE `reflect` record to the trajectory — checkpoints are
deltas (the prompt carries the previous checkpoint's summary), so a hard-killed
session keeps everything up to its last checkpoint and the dream's catch-up
only has to cold-reflect true stragglers. The FINAL reflect also appends an
`outcome` record (source="reflect"): readers take the last outcome, so the
graded score supersedes the heuristic counters without rewriting anything.

The model matches observations against the KNOWN pattern index in-call
(reinforces: name-or-null), so the accumulator downstream stays pure code.
Recurrence is counted per SESSION there, never per checkpoint — three
checkpoints seeing the same habit is one observation, not a hot streak.

Never in bench mode: an engine has no hooks until an entry point attaches one
(attach_from_config: chat / exec / goal / serve), so scores keep measuring the harness.

Contract: run_reflect never raises — any API/parse trouble returns None and
the session goes on; a missed checkpoint just means a colder dream later.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Optional, Sequence

from rockycode.engine import images as images_mod
from rockycode.engine.effort import build_extra_body

ANGLES = ("completion", "adherence", "efficiency", "user_feeling")

# The aggregate lives in code so a score of 0.78 means the same thing next
# month. Completion dominates (the harness exists to finish tasks); feeling
# outweighs efficiency because a happy slow session beats a fast wrong one.
WEIGHTS = {"completion": 0.40, "adherence": 0.25, "efficiency": 0.15, "user_feeling": 0.20}

PATTERN_KINDS = ("habit", "weakness")
MAX_PATTERNS = 3          # per reflect — patterns should be rare and load-bearing
MAX_FACTS = 5
MAX_TOKENS = 1024

REFLECT_FIELDS = """\
Fields:
- grade: score the session so far, each angle 0.0 (bad) to 1.0 (great).
  completion = did it accomplish what the user asked; adherence = followed
  instructions, stayed in scope; efficiency = direct path, few wasted steps;
  user_feeling = what the user's BEHAVIOR suggests (corrections, repeated
  asks, interrupts, thanks, abandonment — no rating exists, infer it).
- episode: task (one line), outcome ("success"|"partial"|"failed"), worked /
  failed (short bullets as string arrays, [] if none).
- facts: durable project facts worth remembering across sessions (paths,
  commands, gotchas), each ONE standalone line. [] if none. Max {max_facts}.
- patterns: recurring behaviors worth tracking, max {max_patterns}, [] is a
  fine answer. kind "habit" = the USER keeps doing some task manually, by
  hand, again (candidate for a skill or a scheduled routine — name the task,
  not the user). kind "weakness" = the AGENT keeps failing the same way
  (include the likely cause in description and one actionable instruction in
  advice). Only patterns this session actually evidences — no speculation.
  If one matches a KNOWN pattern, set reinforces to that name and keep the
  rest short.
- skills_used: names of installed skills actually loaded this session, [].
- summary: 2-4 sentences a future checkpoint can build on (what happened,
  where things stand).

{{"grade": {{"completion": 0.0, "adherence": 0.0, "efficiency": 0.0,
   "user_feeling": 0.0, "rationale": "one or two sentences citing evidence"}},
 "episode": {{"task": "…", "outcome": "…", "worked": [], "failed": []}},
 "facts": [],
 "patterns": [{{"kind": "habit", "reinforces": null, "name": "kebab-case-name",
    "description": "one or two sentences", "advice": "one instruction or ''"}}],
 "skills_used": [],
 "summary": "…"}}
"""

REFLECT_PROMPT = """\
[harness] Reflection checkpoint — this is a side note to the harness, not part \
of the conversation. Review THIS session so far and reply with ONLY a JSON \
object, no prose around it.

{delta}KNOWN PATTERNS (reinforce these by name instead of re-describing them):
{known}

""" + REFLECT_FIELDS

# The COLD variant — dream's catch-up for sessions that ended without a final
# reflect (hard kill, timeout, pre-v2 trajectory). Same JSON contract over a
# condensed stored transcript instead of the live prefix: colder and lossier,
# which is exactly why the warm path exists.
COLD_PROMPT = """\
You are reviewing one FINISHED coding-agent session from its condensed
transcript. Reply with ONLY a JSON object, no prose around it. Infer the
user's experience from behavior alone (corrections, repeated asks,
interrupts, gratitude, abandonment); a trailing [outcome] line holds
mechanical counters — use it as evidence.

TRANSCRIPT:
{transcript}

KNOWN PATTERNS (reinforce these by name instead of re-describing them):
{known}

""" + REFLECT_FIELDS

DELTA_NOTE = """\
PREVIOUS CHECKPOINT (report only what changed since; grade the WHOLE session):
{summary}

"""


def _clamp01(v) -> Optional[float]:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return None


def _str_list(v, limit: int, each: int = 200) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(s).strip()[:each] for s in v if isinstance(s, str) and str(s).strip()][:limit]


def parse_reflection(answer: str) -> Optional[dict]:
    """Lenient JSON extraction with the same all-or-nothing rule the judge
    had for angles: a partial grade would silently skew the aggregate, so any
    missing/non-numeric angle voids the whole reflection. Everything else
    degrades field-by-field (a reflection with no patterns is still a grade)."""
    m = re.search(r"\{.*\}", answer or "", re.DOTALL)
    if m is None:
        return None
    try:
        obj = json.loads(m.group())
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    grade = obj.get("grade")
    if not isinstance(grade, dict):
        return None
    angles: dict[str, float] = {}
    for a in ANGLES:
        v = _clamp01(grade.get(a))
        if v is None:
            return None
        angles[a] = v
    score = round(sum(WEIGHTS[a] * angles[a] for a in ANGLES), 4)

    episode = obj.get("episode") if isinstance(obj.get("episode"), dict) else {}
    outcome = str(episode.get("outcome", "")).strip().lower()
    if outcome not in ("success", "partial", "failed"):
        outcome = "partial"

    patterns = []
    raw = obj.get("patterns") if isinstance(obj.get("patterns"), list) else []
    for it in raw[:MAX_PATTERNS]:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name", "")).strip()
        desc = str(it.get("description", "")).strip()
        if not name or not desc:
            continue  # a pattern without a name and a description is a vibe
        kind = str(it.get("kind", "")).strip().lower()
        reinforces = it.get("reinforces")
        patterns.append({
            "kind": kind if kind in PATTERN_KINDS else "weakness",
            "reinforces": str(reinforces).strip() if isinstance(reinforces, str) and reinforces.strip() else None,
            "name": name[:80],
            "description": desc[:400],
            "advice": str(it.get("advice", "")).strip()[:300],
        })

    return {
        "angles": angles,
        "score": score,
        "rationale": str(grade.get("rationale", "")).strip()[:400],
        "episode": {
            "task": str(episode.get("task", "")).strip()[:200] or "(unknown task)",
            "outcome": outcome,
            "worked": _str_list(episode.get("worked"), 6),
            "failed": _str_list(episode.get("failed"), 6),
        },
        "facts": _str_list(obj.get("facts"), MAX_FACTS),
        "patterns": patterns,
        "skills_used": _str_list(obj.get("skills_used"), 10, each=64),
        "summary": str(obj.get("summary", "")).strip()[:1200],
    }


def build_instruction(*, known_patterns: Sequence[tuple[str, str]],
                      prior_summary: Optional[str]) -> str:
    """The one instruction message appended after the live history. Known
    patterns and the delta note ride HERE, after the shared prefix, so the
    cache hit on everything before them is untouched."""
    known = "\n".join(f"- [{n}] {d}" for n, d in list(known_patterns)[:20]) or "(none)"
    delta = DELTA_NOTE.format(summary=prior_summary.strip()) if prior_summary else ""
    return REFLECT_PROMPT.format(delta=delta, known=known,
                                 max_patterns=MAX_PATTERNS, max_facts=MAX_FACTS)


async def run_reflect(
    client,
    model: str,
    api_messages: list[dict],
    *,
    tools: Optional[list[dict]] = None,
    extra_body: Optional[dict] = None,
    known_patterns: Sequence[tuple[str, str]] = (),
    prior_summary: Optional[str] = None,
    final: bool = False,
    seq: int = 1,
    at_messages: int = 0,
) -> Optional[dict]:
    """One reflection over the session's current api_view. `tools` should be
    the same schemas the main call sends — they are part of the cached request
    prefix — with tool_choice="none" so the model can't answer with a call.
    Returns the parsed record data, or None (API trouble / bad JSON)."""
    messages = list(api_messages) + [
        {"role": "user", "content": build_instruction(
            known_patterns=known_patterns, prior_summary=prior_summary)}
    ]
    try:
        kwargs: dict = {}
        if tools:
            kwargs = {"tools": tools, "tool_choice": "none"}
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=MAX_TOKENS,
            extra_body=extra_body or {},
            **kwargs,
        )
        answer = (resp.choices[0].message.content or "") if resp.choices else ""
    except Exception:  # noqa: BLE001 — a failed checkpoint must never touch the session
        return None
    parsed = parse_reflection(answer)
    if parsed is None:
        return None
    return {
        "seq": seq,
        "final": final,
        "at_messages": at_messages,
        "reflected_at": time.time(),
        "model": model,
        **parsed,
    }


async def run_reflect_cold(
    chat,
    transcript: str,
    *,
    known_patterns: Sequence[tuple[str, str]] = (),
) -> Optional[dict]:
    """Dream's catch-up reflect over a condensed stored transcript. `chat` is
    the prompt-in/text-out adapter dream jobs share (dream/core.CloudChat, or
    a test fake). Same never-raise contract as run_reflect."""
    known = "\n".join(f"- [{n}] {d}" for n, d in list(known_patterns)[:20]) or "(none)"
    try:
        answer = await chat.chat(
            COLD_PROMPT.format(transcript=transcript, known=known,
                               max_patterns=MAX_PATTERNS, max_facts=MAX_FACTS),
            max_tokens=MAX_TOKENS,
        )
    except Exception:  # noqa: BLE001 — a failed catch-up is just a session skipped
        return None
    parsed = parse_reflection(answer)
    if parsed is None:
        return None
    return {"seq": 1, "final": True, "at_messages": 0, "cold": True,
            "reflected_at": time.time(), **parsed}


# ---- the engine hook ---------------------------------------------------------------
#
# The engine knows nothing about reflect: it keeps a list of boundary hooks and
# calls `on_boundary` (sync, at turn end / before a compaction / at a step-limit
# exit) and `on_final` (awaited once, at exit). This hook is the only one today.

REFLECT_MAX_PER_SESSION = 6   # checkpoints; the final reflect rides outside the cap
REFLECT_EVERY = 200
REFLECT_COMPACT_MIN_DELTA = 8
REFLECT_FINAL_TIMEOUT = 15.0  # seconds exit will wait; a miss = dream cold-reflects later


class ReflectHook:
    """Reflect checkpoints on the session model, attached to an Engine's hooks.

    `attach(engine, every=…)` is the opt-in: chat, exec, goal and serve attach
    from config; bench never does, so scores keep measuring the harness. All
    state is hook-local; the records land in the trajectory (source of truth).
    """

    def __init__(self, every: int = REFLECT_EVERY, max_per_session: int = REFLECT_MAX_PER_SESSION) -> None:
        self.every = int(every)
        self.max_per_session = max_per_session
        self.seq = 0
        self.reflected_msgs = 1            # history length at the last reflect
        self.summary: Optional[str] = None
        self.task: Optional[asyncio.Task] = None
        self.final_done = False

    # -- engine contract --

    def on_boundary(self, engine, *, reason: str = "messages",
                    history_view: Optional[list[dict]] = None) -> None:
        """Fire-and-forget a checkpoint when a boundary warrants one. Sync and
        cheap on the no path; the call itself runs as a task so the user never
        waits on it. One in flight at a time — an overlapping boundary is
        simply skipped (its material is still there for the next)."""
        if engine.trajectory.path is None or self.seq >= self.max_per_session:
            return
        if self.task is not None and not self.task.done():
            return
        delta = len(engine.history) - self.reflected_msgs
        if delta < (REFLECT_COMPACT_MIN_DELTA if reason == "compaction" else self.every):
            return
        # Snapshot NOW: a compaction caller is about to rewrite history, and a
        # later-running task must reflect over what the provider just saw.
        view = history_view if history_view is not None else images_mod.api_view(
            engine.history, vision=engine.vision_enabled)
        try:
            self.task = asyncio.get_running_loop().create_task(self._run(engine, view, final=False))
        except RuntimeError:
            pass  # no running loop (sync caller) — the final reflect will cover it

    def wants_final(self, engine) -> bool:
        """Whether on_final would make a call: trivial sessions (no tool call,
        single turn) skip it, same bar the old judge gate used."""
        if self.final_done or engine.trajectory.path is None:
            return False
        return engine.stats.tool_calls >= 1 or engine.stats.turns >= 2

    async def on_final(self, engine, *, timeout: float = REFLECT_FINAL_TIMEOUT) -> Optional[dict]:
        """The end-of-session reflect — awaited by the exit paths with a small
        timeout so leaving never hangs. Call AFTER engine.finalize_outcome() so
        the graded outcome record lands last."""
        if not self.wants_final(engine):
            return None
        self.final_done = True
        # Let an in-flight checkpoint land first — it feeds prior_summary, and
        # two concurrent reflects would race the seq counter.
        if self.task is not None and not self.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=timeout / 2)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
        view = images_mod.api_view(engine.history, vision=engine.vision_enabled)
        try:
            # Not wait_for: on Python 3.11 it runs this as an inner task, which takes
            # one more step (the request) after the exit has cancelled it.
            async with asyncio.timeout(timeout):
                return await self._run(engine, view, final=True)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001 — dream covers stragglers
            return None

    async def cancel(self) -> None:
        """Drop an in-flight checkpoint (teardown of a cancelled host)."""
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    # -- the call --

    async def _run(self, engine, api_messages: list[dict], *, final: bool) -> Optional[dict]:
        """One checkpoint: call, parse, append the `reflect` record. The final
        reflect also appends an outcome record (source="reflect") AFTER the
        heuristic one, so last-outcome-wins upgrades the session to a graded
        score. Never raises; None = no record (dream cold-reflects later)."""
        known: list[tuple[str, str]] = []
        store = getattr(engine, "memory_store", None)
        if store is not None:
            try:
                known = [(m.name, m.description) for m in store.load_all()
                         if m.type in ("weakness", "habit")]
            except Exception:  # noqa: BLE001 — the index is a nicety, not a dependency
                known = []
        data = await run_reflect(
            engine.client, engine.wire_model,
            engine.protocol.messages(api_messages, tools=engine.tools_enabled and bool(engine.registry)),
            tools=[t.schema for t in engine.registry.values()] if engine.tools_enabled else None,
            # thinking off on purpose: a checkpoint is a cheap structured read,
            # shaped for whatever provider is live (same clamp as the main call).
            extra_body=build_extra_body(False, engine.reasoning_effort, engine.reasoning_policy,
                                        efforts=engine.efforts, thinking_off=engine.thinking_off),
            known_patterns=known,
            prior_summary=self.summary,
            final=final,
            seq=self.seq + 1,
            at_messages=len(engine.history),
        )
        if data is None:
            return None
        self.seq += 1
        self.reflected_msgs = len(engine.history)
        self.summary = data.get("summary") or self.summary
        engine.trajectory.reflect(data)
        if final:
            engine.trajectory.outcome({
                "source": "reflect", "seq": data["seq"], "score": data["score"],
                "angles": data["angles"], "rationale": data["rationale"],
            })
        return data


def attached(engine) -> Optional[ReflectHook]:
    """The engine's ReflectHook, if one is attached."""
    for h in getattr(engine, "hooks", ()):
        if isinstance(h, ReflectHook):
            return h
    return None


def attach(engine, *, every: int = REFLECT_EVERY) -> ReflectHook:
    """Opt the engine into reflect (idempotent; re-attaching only updates `every`)."""
    hook = attached(engine)
    if hook is None:
        hook = ReflectHook(every=every)
        engine.hooks.append(hook)
    else:
        hook.every = int(every)
    return hook


def detach(engine) -> None:
    hook = attached(engine)
    if hook is not None:
        engine.hooks.remove(hook)


def attach_from_config(engine, workdir=None) -> Optional[ReflectHook]:
    """Attach when config says `evolve = auto` (the chat / exec / goal / serve
    entry points call this; bench never does)."""
    from rockycode.config import load as _load_config
    cfg = _load_config(workdir) if workdir is not None else _load_config()
    if cfg.get("evolve", "off") != "auto":
        return None
    return attach(engine)
