"""The /loop card — one live loop: pause or resume it, stop it, decide the
approval it's waiting on, or leave it.

Mounted inline by /loop (one card per live loop, walked in order, like the
routine cards). Resolves the Future with "pause" | "resume" | "stop" |
"allow" | "close". Rows are clickable with paired keys: ↑↓ + Enter, p
pause/resume, s stop, a allow (only when the loop is waiting on one), Esc
leave. The title carries the clock face of the last tick so the card reads
like the transcript lines do.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from rich.markup import escape
from textual.containers import Vertical
from textual.widgets import Static

from rockycode.engine.cron import clock_face, fmt_duration
from rockycode.palette import AMBER, LAVENDER, MUTED, VIOLET


class _Row(Static):
    def __init__(self, value: str, idx: int, card: "LoopCard") -> None:
        super().__init__("", id=f"lp-opt-{idx}", classes="lp-opt")
        self._value = value
        self._card = card

    def on_click(self) -> None:
        self._card.pick(self._value)


class LoopCard(Vertical):
    """One live loop. Resolves `future` with the choice."""

    can_focus = True

    BINDINGS = [
        ("up", "move(-1)", "up"),
        ("down", "move(1)", "down"),
        ("enter", "confirm", "select"),
        ("p", "toggle", "pause/resume"),
        ("s", "pick('stop')", "stop"),
        ("a", "pick('allow')", "allow"),
        ("escape", "pick('close')", "leave"),
    ]

    DEFAULT_CSS = """
    LoopCard {
        height: auto;
        margin: 0 1 1 1;
        padding: 1 2;
        background: $surface;
        border: round $primary;
        border-title-color: $text-muted;
        border-subtitle-color: $text-muted;
    }
    LoopCard #lp-desc { color: $text-muted; }
    LoopCard .lp-opt { height: 1; }
    LoopCard #lp-keys { color: $text-muted; margin-top: 1; }
    """

    def __init__(self, loop, sym: str, future: "asyncio.Future[str]") -> None:
        super().__init__()
        self._loop = loop
        self._sym = sym
        self._future = future
        self._idx = 0
        paused = loop.status == "paused"
        choices = []
        if paused and loop.blocked_on:
            choices.append(("allow", "✓  Decide the approval it's waiting on"))
        choices.append(("resume", "▶  Resume") if paused else ("pause", "‖  Pause"))
        choices.append(("stop", "✗  Stop"))
        choices.append(("close", "·  Leave it"))
        self._choices = tuple(choices)

    def compose(self):
        lp = self._loop
        when = (f"next {datetime.fromtimestamp(lp.next_due):%H:%M}" if lp.status == "running"
                else "paused")
        if lp.status == "paused" and lp.blocked_on:
            when = f"[{AMBER}]paused — waiting on: {escape(str(lp.blocked_on.get('detail', ''))[:80])}[/]"
        last = ""
        if lp.ticks:
            last = f" · last: {lp.last_verdict}" + (f" ×{lp.quiet_streak}" if lp.quiet_streak > 1 else "")
        yield Static(
            f"[{MUTED}]“{escape(lp.prompt[:120])}”\n"
            f"every {fmt_duration(lp.interval_s)} · {lp.budget_note(self._sym)} · "
            f"{lp.ticks} tick(s){last} · {self._sym}{lp.spend:.2f} · {when}[/]",
            id="lp-desc",
        )
        for i, (value, _label) in enumerate(self._choices):
            yield _Row(value, i, self)
        allow = f"[{LAVENDER}]a[/] allow · " if any(v == "allow" for v, _ in self._choices) else ""
        yield Static(
            f"[{LAVENDER}]↑↓[/] choose · [{LAVENDER}]↵[/] select · "
            f"[{LAVENDER}]p[/] pause/resume · [{LAVENDER}]s[/] stop · {allow}"
            f"[{LAVENDER}]esc[/] leave · or just click a row",
            id="lp-keys",
        )

    def on_mount(self) -> None:
        lp = self._loop
        face = clock_face(datetime.fromtimestamp(lp.last_tick) if lp.last_tick else datetime.now())
        self.border_title = f"{face} loop #{lp.id} · {lp.status}"
        self.border_subtitle = "ticks never prompt — a loop lives only while rocky runs"
        self._render_rows()
        self.focus()

    def _render_rows(self) -> None:
        for i, (_v, label) in enumerate(self._choices):
            row = self.query_one(f"#lp-opt-{i}", Static)
            row.update(f"[b {VIOLET}]▸ {label}[/]" if i == self._idx else f"[{MUTED}]  {label}[/]")

    def action_move(self, delta: int) -> None:
        self._idx = (self._idx + delta) % len(self._choices)
        self._render_rows()

    def action_confirm(self) -> None:
        self.pick(self._choices[self._idx][0])

    def action_toggle(self) -> None:
        self.pick("resume" if self._loop.status == "paused" else "pause")

    def action_pick(self, choice: str) -> None:
        if choice == "allow" and not any(v == "allow" for v, _ in self._choices):
            return  # nothing to decide — the key is a no-op, not a crash
        self.pick(choice)

    def pick(self, value: str) -> None:
        """Idempotent — a stray second click/key must not crash the Future."""
        if not self._future.done():
            self._future.set_result(value)
