"""The routine due card — run it, lease it, or turn it off.

Mounted inline by /routines, one card per due routine. Resolves the Future
with "run" | "auto" | "later" | "off". The auto choice grants (or renews)
the 7-day lease with the routine's lease budget — the label always shows
what the lease costs, and on renewal what the last lease actually spent,
so trust is re-earned with the bill in view (locked design 2026-07-17).

Rows are clickable; ↑↓ + Enter picks one, Esc = later. No letter answers
it: what the user types is their message (rockycode/tui/cardkeys.py).
"""
from __future__ import annotations

import asyncio

from rich.markup import escape
from textual.containers import Vertical
from textual.widgets import Static

from rockycode.i18n import tr, resolve_locale
from rockycode.palette import LAVENDER, MUTED, VIOLET
from rockycode.tui.cardkeys import CardKeys


class _Row(Static):
    def __init__(self, value: str, idx: int, card: "RoutineCard") -> None:
        super().__init__("", id=f"rt-opt-{idx}", classes="rt-opt")
        self._value = value
        self._card = card

    def on_click(self) -> None:
        self._card.pick(self._value)


class RoutineCard(CardKeys, Vertical):
    """One due routine. Resolves `future` with the choice."""

    can_focus = True

    BINDINGS = [
        ("up", "move(-1)", "up"),
        ("down", "move(1)", "down"),
        ("enter", "confirm", "select"),
        ("escape", "pick('later')", "later"),
    ]

    DEFAULT_CSS = """
    RoutineCard {
        height: auto;
        margin: 0 1 1 1;
        padding: 1 2;
        background: $surface;
        border: round $panel-lighten-2;
        border-title-color: $text-muted;
        border-subtitle-color: $text-muted;
    }
    RoutineCard:focus { border: round $primary; }
    RoutineCard #rt-desc { color: $text-muted; }
    RoutineCard .rt-opt { height: auto; min-height: 1; text-opacity: 55%; }
    RoutineCard:focus .rt-opt { text-opacity: 100%; }
    RoutineCard #rt-keys { color: $text-muted; margin-top: 1; }
    """

    KEYS_ID = "#rt-keys"

    def __init__(self, routine, lease_spent: float, future: "asyncio.Future[str]", *, language: str = "en") -> None:
        self._language = resolve_locale(language)
        super().__init__()
        self._r = routine
        self._lease_spent = lease_spent
        self._future = future
        self._idx = 0
        auto_label = tr("widgets.routine.renew" if routine.auto else "widgets.routine.auto", self._language,
                        budget=routine.budget_lease, spent=lease_spent)
        self._choices = (
            ("run", tr("widgets.routine.run", self._language)),
            ("auto", auto_label),
            ("later", tr("widgets.routine.later", self._language)),
            ("off", tr("widgets.routine.off", self._language)),
        )

    def compose(self):
        r = self._r
        grants = escape(", ".join(r.tools)) or tr("widgets.none", self._language)
        yield Static(tr("widgets.routine.description", self._language, colour=MUTED,
                        description=escape(r.description or r.prompt[:100]), cadence=escape(r.cadence),
                        network=tr("widgets.on" if r.network else "widgets.off", self._language),
                        grants=grants, steps=r.max_steps), id="rt-desc")
        for i, (value, _label) in enumerate(self._choices):
            yield _Row(value, i, self)
        yield Static(self._keys_markup(), id="rt-keys")

    def _keys_markup(self) -> str:
        return tr("widgets.routine.keys", self._language, colour=LAVENDER)

    def on_mount(self) -> None:
        self.border_title = tr("widgets.routine.title", self._language, name=escape(self._r.name))
        self.border_subtitle = tr("widgets.routine.subtitle", self._language)
        self._render_rows()

    def _render_rows(self) -> None:
        for i, (_v, label) in enumerate(self._choices):
            row = self.query_one(f"#rt-opt-{i}", Static)
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
