"""Loops — the in-session cron behind /loop (alias /cron) and the loop_start
/ loop_stop tools. Pure core: no asyncio, no Textual, no I/O.

A loop re-fires ONE prompt into the live session on an interval. Each tick is
an ordinary turn on the session engine (it sees the whole conversation), run
by the host's pump only when the session is idle. This module owns the
bookkeeping — what is due, what a tick's marker says, how a reply is read,
what a user-set budget means — and nothing about how a tick is driven or
shown; the TUI (and later serve) does that. Design: docs/loop-design.md.

Positioning: /routines is the CROSS-launch scheduler (daily/weekly, catch-up
at launch, sandboxed, own context). A loop is in-session: minutes, this
chat, this permission mode, and it dies with the session.

Bounds (cici, 2026-09-30): NO cap is applied that the user did not set.
`for`, `x<count>` and `max $` are budgets only when typed; otherwise a loop
runs until /loop stop, LOOP DONE, loop_stop, or session end. Instead of
caps there are soft reminders — muted lines, at most one an hour per loop,
when a wall-clock or spend mark is crossed. The only floor is the interval:
a tick re-reads the whole prompt prefix, so `5s` is a typo, not a wish.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Optional

from rockycode.engine.tools import Tool, _fn_schema

MIN_INTERVAL_S = 60.0
REMIND_HOURS = (1, 2, 4, 8, 16, 32, 64, 128)      # doubling wall-clock marks
REMIND_SPEND_STEP = {"usd": 0.50, "cny": 5.0}     # per-currency spend marks
REMIND_MIN_GAP_S = 3600.0                         # ≤ one reminder per hour per loop

QUIET, NOTE, DONE = "quiet", "note", "done"

# The per-tick rules. BYTE-STABLE text (only the bracketed header varies) so
# the model sees the same contract every tick; it rides the USER turn, never
# the system prompt (plan-mode precedent — the cached prefix stays identical).
TICK_RULES = (
    "This is an automatic check-in; the user may be away. Do only what the "
    "prompt asks and keep it short. If a tool call is denied for needing "
    "approval, do not retry it — report it. End your reply with exactly one "
    "line:\n"
    "LOOP QUIET — nothing changed, nothing to report\n"
    "LOOP NOTE — something the user should see (say what, briefly)\n"
    "LOOP DONE — the loop's purpose is fulfilled; stop it"
)

# ─────────────────────────────────────────────────────────────────────────────
# parsing — intervals, durations, the /loop argument line
# ─────────────────────────────────────────────────────────────────────────────

_UNITS = {
    "s": 1.0, "sec": 1.0, "secs": 1.0,
    "m": 60.0, "min": 60.0, "mins": 60.0,
    "h": 3600.0, "hr": 3600.0, "hrs": 3600.0,
    "d": 86400.0, "day": 86400.0, "days": 86400.0,
}
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([a-z]+)$", re.I)
_COUNT = re.compile(r"^[x×](\d+)$", re.I)
_MONEY = re.compile(r"^[$¥￥]?(\d+(?:\.\d+)?)$")


def parse_duration(token: str) -> Optional[float]:
    """'5m' → 300.0; '1h' → 3600.0; '90s', '2d', '10min' … None if not a duration."""
    m = _DURATION.match(token.strip())
    if not m:
        return None
    unit = _UNITS.get(m.group(2).lower())
    if unit is None:
        return None
    return float(m.group(1)) * unit


def fmt_duration(seconds: float) -> str:
    """300 → '5m' · 3600 → '1h' · 5400 → '1h30m' · 90 → '90s' · 86400 → '1d'."""
    s = int(round(seconds))
    if s >= 86400 and s % 86400 == 0:
        return f"{s // 86400}d"
    if s >= 3600:
        h, rem = divmod(s, 3600)
        if rem == 0:
            return f"{h}h"
        if rem % 60 == 0:
            return f"{h}h{rem // 60}m"
        return f"{h}h{rem // 60}m{rem % 60}s"
    if s >= 60 and s % 60 == 0:
        return f"{s // 60}m"
    return f"{s}s"


def parse_loop_args(text: str) -> tuple[Optional[dict], str]:
    """The line after `/loop`: `<interval> [for <dur>] [x<N>] [max $<amt>] <prompt>`.

    Budget clauses may come in any order and are consumed only while they
    parse as clauses — the first token that is not one starts the prompt, so
    `/loop 5m for the record, check …` keeps "for the record" as prompt text.
    Returns ({interval_s, for_s, count, max_spend, prompt}, "") or (None, error).
    """
    toks = text.strip().split()
    if not toks:
        return None, "usage: /loop <interval> [for <dur>] [x<N>] [max $<amt>] <prompt>"
    interval = parse_duration(toks[0])
    if interval is None:
        return None, f"'{toks[0]}' is not an interval — try 5m, 30m, 1h"
    if interval < MIN_INTERVAL_S:
        return None, f"the shortest interval is 1m (got {toks[0]}) — a tick re-reads the whole conversation"
    spec: dict = {"interval_s": interval, "for_s": None, "count": None, "max_spend": None}
    i = 1
    while i < len(toks):
        t = toks[i].lower()
        if t == "for" and i + 1 < len(toks):
            d = parse_duration(toks[i + 1])
            if d is None:
                break                     # "for the record…" — prompt text
            if d <= 0:
                return None, "`for` needs a positive duration"
            spec["for_s"], i = d, i + 2
            continue
        mc = _COUNT.match(t)
        if mc:
            n = int(mc.group(1))
            if n <= 0:
                return None, "`x<N>` needs a positive count"
            spec["count"], i = n, i + 1
            continue
        if t == "max" and i + 1 < len(toks):
            mm = _MONEY.match(toks[i + 1])
            if mm is None:
                break
            amt = float(mm.group(1))
            if amt <= 0:
                return None, "`max` needs a positive amount"
            spec["max_spend"], i = amt, i + 2
            continue
        break
    prompt = " ".join(toks[i:]).strip()
    if not prompt:
        return None, "what should each tick do? /loop 5m <prompt>"
    spec["prompt"] = prompt
    return spec, ""


# ─────────────────────────────────────────────────────────────────────────────
# the clock
# ─────────────────────────────────────────────────────────────────────────────

def clock_face(at: datetime) -> str:
    """The clock-face emoji nearest *at*, half-hour resolution: 🕐…🕛 on the
    hour (U+1F550–1F55B), 🕜…🕧 on the half (U+1F55C–1F567). 14:35 → 🕝."""
    slot = round((at.hour * 60 + at.minute) / 30) % 48   # 0 = 12:00
    hour12 = (slot // 2) % 12
    idx = (12 if hour12 == 0 else hour12) - 1
    return chr((0x1F55C if slot % 2 else 0x1F550) + idx)


# ─────────────────────────────────────────────────────────────────────────────
# the tick contract — marker in, verdict out
# ─────────────────────────────────────────────────────────────────────────────

def tick_marker(loop: "Loop", tick_no: int, at: datetime) -> str:
    """The user-turn text for one tick: a bracketed header + the prompt + the
    byte-stable rules."""
    return (f"[loop #{loop.id} · tick {tick_no} · every {fmt_duration(loop.interval_s)} · "
            f"{at:%H:%M}] {loop.prompt}\n\n{TICK_RULES}")


_VERDICT = re.compile(r"LOOP\s+(QUIET|NOTE|DONE)\b[\s*_]*(?:[—–:-]+\s*(.*))?", re.I)


def parse_tick_reply(text: str) -> tuple[str, str]:
    """(verdict, message). The LAST marker in the reply wins (the model may
    quote the rules before deciding). No marker → NOTE: fail-visible, never
    fail-silent. `message` is whatever followed the dash, for the done line."""
    last = None
    for m in _VERDICT.finditer(text or ""):
        last = m
    if last is None:
        return NOTE, ""
    verdict = last.group(1).lower()
    msg = (last.group(2) or "").strip().strip("*").strip()
    return verdict, msg.splitlines()[0] if msg else ""


def tick_cost_estimate(prompt_tokens: int, rate: dict, completion_tokens: int = 400) -> float:
    """Rough cost of one tick in the ledger's currency: the whole prefix at the
    cache-HIT rate (a loop keeps it warm) + the marker at miss + a short
    completion. For the receipt line; never a cap."""
    marker_tokens = 300
    return (prompt_tokens * rate.get("in_hit", 0.0)
            + marker_tokens * rate.get("in_miss", 0.0)
            + completion_tokens * rate.get("out", 0.0)) / 1e6


# ─────────────────────────────────────────────────────────────────────────────
# the book — every loop in this session
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Loop:
    id: int
    prompt: str
    interval_s: float
    created: float
    # user-set budget — None means none; enforced only when set
    for_s: Optional[float] = None
    count: Optional[int] = None
    max_spend: Optional[float] = None
    # runtime
    status: str = "running"          # running | paused | stopped | done
    ticks: int = 0
    quiet_streak: int = 0
    last_tick: float = 0.0           # when the last tick started (0 = never)
    next_due: float = 0.0
    spend: float = 0.0
    last_verdict: str = ""
    last_message: str = ""
    blocked_on: Optional[dict] = None  # the denied call that paused it
    end_reason: str = ""
    # reminders
    last_reminder: float = 0.0
    reminded_hours: int = 0
    reminded_spend: float = 0.0
    origin: str = "user"             # user | model

    @property
    def live(self) -> bool:
        return self.status in ("running", "paused")

    def budget_note(self, sym: str) -> str:
        parts = []
        if self.for_s is not None:
            parts.append(f"for {fmt_duration(self.for_s)}")
        if self.count is not None:
            parts.append(f"x{self.count}")
        if self.max_spend is not None:
            parts.append(f"max {sym}{self.max_spend:g}")
        return " · ".join(parts) if parts else "no budget"


class LoopBook:
    """Session-scoped registry. Nothing here persists — a loop is session
    state by design (no file can ship one, none survives a launch)."""

    def __init__(self, currency: str = "usd") -> None:
        self.currency = currency
        self._loops: dict[int, Loop] = {}
        self._seq = 0
        self.tick_active: Optional[int] = None   # loop id whose tick is running
        # The host's hook for loop_start / loop_stop: async (kind, loop) → None,
        # called AFTER the book changed so the receipt / stop line renders. Set
        # by the TUI at mount (the CLI builds the tools before the app exists).
        self.notify: Optional[Callable[[str, "Loop"], Awaitable[None]]] = None

    @property
    def sym(self) -> str:
        return "¥" if self.currency == "cny" else "$"

    # -- create / find --------------------------------------------------------

    def add(self, prompt: str, interval_s: float, *, for_s: Optional[float] = None,
            count: Optional[int] = None, max_spend: Optional[float] = None,
            origin: str = "user", now: Optional[float] = None) -> Loop:
        now = time.time() if now is None else now
        self._seq += 1
        loop = Loop(id=self._seq, prompt=prompt.strip(), interval_s=float(interval_s),
                    created=now, for_s=for_s, count=count, max_spend=max_spend,
                    next_due=now + float(interval_s), origin=origin)
        self._loops[loop.id] = loop
        return loop

    def get(self, loop_id: int) -> Optional[Loop]:
        return self._loops.get(loop_id)

    def all(self) -> list[Loop]:
        return list(self._loops.values())

    def live(self) -> list[Loop]:
        return [lp for lp in self._loops.values() if lp.live]

    def running(self) -> list[Loop]:
        return [lp for lp in self._loops.values() if lp.status == "running"]

    def resolve(self, token: Optional[str]) -> tuple[Optional[Loop], str]:
        """A loop from a user-typed id; the id is optional when exactly one
        loop is live. Returns (loop, error) — exactly one is set."""
        live = self.live()
        if token is None or token == "":
            if not live:
                return None, "no loops running — /loop 5m <prompt> starts one"
            if len(live) == 1:
                return live[0], ""
            ids = ", ".join(f"#{lp.id}" for lp in live)
            return None, f"which one? {ids} — /loop shows them"
        tok = token.lstrip("#")
        if not tok.isdigit():
            return None, f"'{token}' is not a loop id"
        lp = self._loops.get(int(tok))
        if lp is None or not lp.live:
            return None, f"no live loop #{tok}"
        return lp, ""

    # -- lifecycle ------------------------------------------------------------

    def stop(self, loop: Loop, reason: str = "stopped") -> Loop:
        if loop.live:
            loop.status = "stopped"
            loop.end_reason = reason
        return loop

    def pause(self, loop: Loop, blocked_on: Optional[dict] = None) -> Loop:
        if loop.status == "running":
            loop.status = "paused"
            loop.blocked_on = blocked_on
        return loop

    def resume(self, loop: Loop, now: Optional[float] = None) -> Loop:
        """Back to running; the next tick fires at the next idle moment (a
        grant or a resume should not wait a whole interval to take effect)."""
        if loop.status == "paused":
            loop.status = "running"
            loop.blocked_on = None
            loop.next_due = time.time() if now is None else now
        return loop

    def resume_blocked(self, now: Optional[float] = None) -> list["Loop"]:
        """Every loop paused FOR AN APPROVAL back to running, due now — what a
        switch to yolo means: nothing is left to approve. A manual /loop
        pause carries no blocked_on and is the user's own decision; it stays
        paused until they say otherwise. Returns the loops resumed."""
        out = [lp for lp in self._loops.values() if lp.status == "paused" and lp.blocked_on]
        for lp in out:
            self.resume(lp, now=now)
        return out

    # -- due + ticks ----------------------------------------------------------

    def due(self, now: Optional[float] = None) -> list[Loop]:
        """Running loops whose interval has elapsed, oldest first. A loop is
        due ONCE no matter how long the session was busy — missed ticks never
        stack (routines' rule)."""
        now = time.time() if now is None else now
        if self.tick_active is not None:
            return []
        return [lp for lp in self._loops.values()
                if lp.status == "running" and lp.next_due <= now]

    def next_due(self, now: Optional[float] = None) -> Optional[Loop]:
        running = self.running()
        return min(running, key=lambda lp: lp.next_due) if running else None

    def begin_tick(self, loop: Loop, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        loop.ticks += 1
        loop.last_tick = now
        self.tick_active = loop.id
        return loop.ticks

    def end_tick(self, loop: Loop, verdict: str, cost: float = 0.0, message: str = "",
                 now: Optional[float] = None) -> Optional[str]:
        """Settle one tick: spend, streak, the verdict, the next due time
        (interval AFTER the tick — "5 minutes of rest between checks"), and
        the user's own budget. Returns the reason the loop ended, or None."""
        now = time.time() if now is None else now
        if self.tick_active == loop.id:
            self.tick_active = None
        loop.spend += max(0.0, float(cost))
        loop.last_verdict = verdict
        loop.last_message = message
        loop.quiet_streak = loop.quiet_streak + 1 if verdict == QUIET else 0
        loop.next_due = now + loop.interval_s
        if loop.status != "running":
            return None                    # cancelled / stopped mid-tick: nothing more
        if verdict == DONE:
            loop.status, loop.end_reason = "done", message or "done"
            return loop.end_reason
        reason = self._budget_hit(loop, now)
        if reason:
            loop.status, loop.end_reason = "stopped", reason
        return reason

    def _budget_hit(self, loop: Loop, now: float) -> Optional[str]:
        """Only what the user set counts; nothing else ever ends a loop."""
        if loop.count is not None and loop.ticks >= loop.count:
            return f"x{loop.count} ticks reached"
        if loop.for_s is not None and now - loop.created >= loop.for_s:
            return f"{fmt_duration(loop.for_s)} reached"
        if loop.max_spend is not None and loop.spend >= loop.max_spend:
            return f"max {self.sym}{loop.max_spend:g} reached"
        return None

    # -- soft reminders -------------------------------------------------------

    def reminder_due(self, loop: Loop, now: Optional[float] = None) -> Optional[dict]:
        """A running-cost reminder, if a mark was crossed since the last one:
        wall-clock at 1h, 2h, 4h, 8h… or spend at each $0.50 / ¥5 step. At
        most one per hour per loop, only for loops with no budget of that
        kind (a user who set `for 3h` doesn't need hour marks), never for a
        loop that is not running. Returns {hours, ticks, spend} or None."""
        now = time.time() if now is None else now
        if loop.status != "running":
            return None
        if loop.last_reminder and now - loop.last_reminder < REMIND_MIN_GAP_S:
            return None                    # the gap is between reminders, not from creation
        hours = (now - loop.created) / 3600.0
        hour_mark = 0
        if loop.for_s is None:
            hour_mark = max((h for h in REMIND_HOURS if h <= hours), default=0)
        spend_mark = 0.0
        if loop.max_spend is None:
            step = REMIND_SPEND_STEP.get(self.currency, 0.5)
            spend_mark = math.floor(loop.spend / step + 1e-9) * step
        crossed = hour_mark > loop.reminded_hours or spend_mark > loop.reminded_spend + 1e-9
        if not crossed:
            return None
        loop.reminded_hours = max(loop.reminded_hours, hour_mark)
        loop.reminded_spend = max(loop.reminded_spend, spend_mark)
        loop.last_reminder = now
        return {"hours": hours, "ticks": loop.ticks, "spend": loop.spend}


# ─────────────────────────────────────────────────────────────────────────────
# the model-facing tools — loop_start / loop_stop
# ─────────────────────────────────────────────────────────────────────────────

LOOP_START_SCHEMA = _fn_schema(
    "loop_start",
    "Start a loop: re-run a prompt in THIS conversation every interval as an "
    "automatic check-in (e.g. \"check whether the bench run in results/ has "
    "finished; if so summarize it\"). Use when the user asks to check on, poll, "
    "keep doing, or come back to something periodically. Each tick sees the "
    "whole conversation and ends with LOOP QUIET / NOTE / DONE. Pass a budget "
    "(for / count / max_spend) ONLY when the user asked for one; otherwise the "
    "loop runs until the user stops it or a tick reports LOOP DONE.",
    {
        "interval": {"type": "string", "description": "How often, e.g. '5m', '30m', '1h' (minimum 1m)."},
        "prompt": {"type": "string", "description": "What each tick should do — self-contained, "
                   "with what counts as done."},
        "for": {"type": "string", "description": "Optional duration budget, e.g. '3h' — only if the user asked."},
        "count": {"type": "integer", "description": "Optional tick-count budget — only if the user asked."},
        "max_spend": {"type": "number", "description": "Optional spend ceiling in the session currency — only if the user asked."},
    },
    ["interval", "prompt"],
)

LOOP_STOP_SCHEMA = _fn_schema(
    "loop_stop",
    "Stop a running loop by id (the id came from loop_start or /loop). Use when "
    "the user says to stop checking / stop the loop.",
    {"id": {"type": "integer", "description": "The loop id."}},
    ["id"],
)

_NO_BREED = ("[error] loops can't be started from inside a tick. If this loop's "
             "purpose is fulfilled, end your reply with LOOP DONE.")
_NO_KILL = ("[error] a tick can't stop loops. End THIS loop with LOOP DONE when "
            "its purpose is fulfilled; the user stops others with /loop stop.")

def build_loop_tools(book: LoopBook) -> dict[str, Tool]:
    """loop_start (risky → the normal approval prompt in ask/careful, so the
    approval IS the receipt; runs in yolo) and loop_stop (safe: harmless and
    visible). Both refuse during a tick — loops don't breed and don't kill
    each other; a tick ends itself with LOOP DONE, nothing else. The host's
    `book.notify` hook (read at call time) renders the receipt / stop line."""

    async def _start(interval: str, prompt: str, **extra) -> str:
        if book.tick_active is not None:
            return _NO_BREED
        secs = parse_duration(str(interval))
        if secs is None:
            return f"[error] '{interval}' is not an interval — use e.g. '5m', '1h'"
        if secs < MIN_INTERVAL_S:
            return "[error] the shortest interval is 1m"
        if not str(prompt).strip():
            return "[error] prompt is empty"
        for_s = None
        if extra.get("for"):
            for_s = parse_duration(str(extra["for"]))
            if for_s is None or for_s <= 0:
                return f"[error] 'for' must be a duration like '3h' (got {extra['for']!r})"
        count = extra.get("count")
        if count is not None:
            try:
                count = int(count)
            except (TypeError, ValueError):
                return "[error] count must be an integer"
            if count <= 0:
                return "[error] count must be positive"
        max_spend = extra.get("max_spend")
        if max_spend is not None:
            try:
                max_spend = float(max_spend)
            except (TypeError, ValueError):
                return "[error] max_spend must be a number"
            if max_spend <= 0:
                return "[error] max_spend must be positive"
        loop = book.add(str(prompt), secs, for_s=for_s, count=count, max_spend=max_spend,
                        origin="model")
        if book.notify is not None:
            await book.notify("start", loop)
        return (f"loop #{loop.id} started — every {fmt_duration(secs)} · "
                f"{loop.budget_note(book.sym)} · first tick in {fmt_duration(secs)}. "
                f"The user can pause or stop it with /loop.")

    async def _stop(id: int) -> str:  # noqa: A002 — the schema's field name
        if book.tick_active is not None:
            return _NO_KILL
        try:
            lid = int(id)
        except (TypeError, ValueError):
            return "[error] id must be an integer"
        loop = book.get(lid)
        if loop is None or not loop.live:
            return f"[error] no live loop #{lid}"
        book.stop(loop, "stopped by rocky at the user's request")
        if book.notify is not None:
            await book.notify("stop", loop)
        return f"loop #{loop.id} stopped after {loop.ticks} tick(s)."

    return {
        "loop_start": Tool(name="loop_start", schema=LOOP_START_SCHEMA, fn=_start, risk="risky"),
        "loop_stop": Tool(name="loop_stop", schema=LOOP_STOP_SCHEMA, fn=_stop, risk="safe"),
    }
