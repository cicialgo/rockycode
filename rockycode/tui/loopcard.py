"""The /loop card — one live loop: pause or resume it, stop it, decide the
approval it's waiting on, or leave it.

Mounted inline by /loop (one card per live loop, walked in order, like the
routine cards). Resolves the Future with "pause" | "resume" | "stop" |
"allow" | "close". Rows are clickable; ↑↓ + Enter picks one, Esc leaves.
No letter answers it: what the user types is their message
(rockycode/tui/cardkeys.py). The title carries the clock face of the last
tick so the card reads like the transcript lines do.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from rich.markup import escape
from textual.containers import Vertical
from textual.widgets import Static

from rockycode.engine.cron import clock_face, fmt_duration
from rockycode.i18n import tr, resolve_locale
from rockycode.palette import AMBER, LAVENDER, MUTED, VIOLET
from rockycode.tui.cardkeys import CardKeys


class _Row(Static):
    def __init__(self, value: str, idx: int, card: "LoopCard") -> None:
        super().__init__("", id=f"lp-opt-{idx}", classes="lp-opt")
        self._value = value
        self._card = card

    def on_click(self) -> None:
        self._card.pick(self._value)


class LoopCard(CardKeys, Vertical):
    """One live loop. Resolves `future` with the choice."""

    can_focus = True

    BINDINGS = [
        ("up", "move(-1)", "up"),
        ("down", "move(1)", "down"),
        ("enter", "confirm", "select"),
        ("escape", "pick('close')", "leave"),
    ]

    DEFAULT_CSS = """
    LoopCard {
        height: auto;
        margin: 0 1 1 1;
        padding: 1 2;
        background: $surface;
        border: round $panel-lighten-2;
        border-title-color: $text-muted;
        border-subtitle-color: $text-muted;
    }
    LoopCard:focus { border: round $primary; }
    LoopCard #lp-desc { color: $text-muted; }
    LoopCard .lp-opt { height: auto; min-height: 1; text-opacity: 55%; }
    LoopCard:focus .lp-opt { text-opacity: 100%; }
    LoopCard #lp-keys { color: $text-muted; margin-top: 1; }
    """

    KEYS_ID = "#lp-keys"

    def __init__(self, loop, sym: str, future: "asyncio.Future[str]", *, language: str = "en") -> None:
        self._language = resolve_locale(language)
        super().__init__()
        self._loop = loop
        self._sym = sym
        self._future = future
        self._idx = 0
        paused = loop.status == "paused"
        choices = []
        if paused and loop.blocked_on:
            choices.append(("allow", tr("widgets.loop.allow", self._language)))
        choices.append(("resume", tr("widgets.loop.resume", self._language)) if paused else ("pause", tr("widgets.loop.pause", self._language)))
        choices.append(("stop", tr("widgets.loop.stop", self._language)))
        choices.append(("close", tr("widgets.loop.leave", self._language)))
        self._choices = tuple(choices)

    def _budget_note(self) -> str:
        lp = self._loop
        parts = []
        if lp.for_s is not None:
            parts.append(tr("widgets.loop.duration", self._language, duration=fmt_duration(lp.for_s)))
        if lp.count is not None:
            parts.append(f"x{lp.count}")
        if lp.max_spend is not None:
            parts.append(tr("widgets.loop.max_spend", self._language, symbol=self._sym, amount=lp.max_spend))
        return " · ".join(parts) if parts else tr("widgets.loop.no_budget", self._language)

    def compose(self):
        lp = self._loop
        when = (tr("widgets.loop.next", self._language, time=f"{datetime.fromtimestamp(lp.next_due):%H:%M}") if lp.status == "running"
                else tr("widgets.loop.paused", self._language))
        if lp.status == "paused" and lp.blocked_on:
            when = tr("widgets.loop.waiting", self._language, colour=AMBER, detail=escape(str(lp.blocked_on.get("detail", ""))[:80]))
        last = ""
        if lp.ticks:
            last = tr("widgets.loop.last", self._language, verdict=escape(lp.last_verdict)) + (f" ×{lp.quiet_streak}" if lp.quiet_streak > 1 else "")
        yield Static(tr("widgets.loop.description", self._language, colour=MUTED,
                        prompt=escape(lp.prompt[:120]), interval=fmt_duration(lp.interval_s),
                        budget=escape(self._budget_note()), ticks=lp.ticks, last=last,
                        sym=self._sym, spend=lp.spend, when=when), id="lp-desc")
        for i, (value, _label) in enumerate(self._choices):
            yield _Row(value, i, self)
        yield Static(self._keys_markup(), id="lp-keys")

    def _keys_markup(self) -> str:
        return tr("widgets.loop.keys", self._language, colour=LAVENDER)

    def on_mount(self) -> None:
        lp = self._loop
        face = clock_face(datetime.fromtimestamp(lp.last_tick) if lp.last_tick else datetime.now())
        self.border_title = tr("widgets.loop.title", self._language, face=face, id=lp.id, status=tr("widgets.loop." + lp.status, self._language) if lp.status in ("running", "paused", "stopped") else lp.status)
        self.border_subtitle = tr("widgets.loop.subtitle", self._language)
        self._render_rows()

    def _render_rows(self) -> None:
        for i, (_v, label) in enumerate(self._choices):
            row = self.query_one(f"#lp-opt-{i}", Static)
            row.update(f"[b {VIOLET}]▸ {label}[/]" if i == self._lit() else f"[{MUTED}]  {label}[/]")

    def action_move(self, delta: int) -> None:
        if self._move(delta, len(self._choices)):
            self._render_rows()

    def _confirm(self) -> None:
        self.pick(self._choices[self._idx][0])

    def action_pick(self, choice: str) -> None:
        self.pick(choice)

    def pick(self, value: str) -> None:
        """Idempotent — a stray second click/key must not crash the Future."""
        if not self._future.done():
            self._future.set_result(value)
