"""The routine due card — run it, lease it, or turn it off.

Mounted inline by /routines, one card per due routine. Resolves the Future
with "run" | "auto" | "later" | "off". The auto choice grants (or renews)
the 7-day lease with the routine's lease budget — the label always shows
what the lease costs, and on renewal what the last lease actually spent,
so trust is re-earned with the bill in view (locked design 2026-07-17).

Rows are clickable with paired keys: ↑↓ + Enter, y run, a auto, n off,
Esc later.
"""
from __future__ import annotations

import asyncio

from textual.containers import Vertical
from textual.widgets import Static

from rockycode.palette import LAVENDER, MUTED, VIOLET


class _Row(Static):
    def __init__(self, value: str, idx: int, card: "RoutineCard") -> None:
        super().__init__("", id=f"rt-opt-{idx}", classes="rt-opt")
        self._value = value
        self._card = card

    def on_click(self) -> None:
        self._card.pick(self._value)


class RoutineCard(Vertical):
    """One due routine. Resolves `future` with the choice."""

    can_focus = True

    BINDINGS = [
        ("up", "move(-1)", "up"),
        ("down", "move(1)", "down"),
        ("enter", "confirm", "select"),
        ("y", "pick('run')", "run"),
        ("a", "pick('auto')", "auto"),
        ("n", "pick('off')", "off"),
        ("escape", "pick('later')", "later"),
    ]

    DEFAULT_CSS = """
    RoutineCard {
        height: auto;
        margin: 0 1 1 1;
        padding: 1 2;
        background: $surface;
        border: round $primary;
        border-title-color: $text-muted;
        border-subtitle-color: $text-muted;
    }
    RoutineCard #rt-desc { color: $text-muted; }
    RoutineCard .rt-opt { height: 1; }
    RoutineCard #rt-keys { color: $text-muted; margin-top: 1; }
    """

    def __init__(self, routine, lease_spent: float, future: "asyncio.Future[str]") -> None:
        super().__init__()
        self._r = routine
        self._lease_spent = lease_spent
        self._future = future
        self._idx = 0
        auto_label = (
            f"↻  Renew auto — 7 days, ${routine.budget_lease:.2f} lease "
            f"(last lease spent ${lease_spent:.2f})"
            if routine.auto else
            f"↻  Auto for 7 days (lease · budget ${routine.budget_lease:.2f})"
        )
        self._choices = (
            ("run", "▶  Run now (sandboxed)"),
            ("auto", auto_label),
            ("later", "·  Later"),
            ("off", "✗  Turn off"),
        )

    def compose(self):
        r = self._r
        grants = ", ".join(r.tools) or "none"
        yield Static(
            f"[{MUTED}]{r.description or r.prompt[:100]}\n"
            f"cadence: {r.cadence} · network: {'on' if r.network else 'off'} · "
            f"grants: {grants} · ≤{r.max_steps} steps[/]",
            id="rt-desc",
        )
        for i, (value, _label) in enumerate(self._choices):
            yield _Row(value, i, self)
        yield Static(
            f"[{LAVENDER}]↑↓[/] choose · [{LAVENDER}]↵[/] select · "
            f"[{LAVENDER}]y[/] run · [{LAVENDER}]a[/] auto · [{LAVENDER}]n[/] off · "
            f"[{LAVENDER}]esc[/] later · or just click a row",
            id="rt-keys",
        )

    def on_mount(self) -> None:
        self.border_title = f"⏰ routine due · {self._r.name}"
        self.border_subtitle = "runs only when you say — auto is a lease, not a switch"
        self._render_rows()
        self.focus()

    def _render_rows(self) -> None:
        for i, (_v, label) in enumerate(self._choices):
            row = self.query_one(f"#rt-opt-{i}", Static)
            row.update(f"[b {VIOLET}]▸ {label}[/]" if i == self._idx else f"[{MUTED}]  {label}[/]")

    def action_move(self, delta: int) -> None:
        self._idx = (self._idx + delta) % len(self._choices)
        self._render_rows()

    def action_confirm(self) -> None:
        self.pick(self._choices[self._idx][0])

    def action_pick(self, choice: str) -> None:
        self.pick(choice)

    def pick(self, value: str) -> None:
        """Idempotent — a stray second click/key must not crash the Future."""
        if not self._future.done():
            self._future.set_result(value)
