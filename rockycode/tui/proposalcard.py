"""The proposal review card — one dream-drafted skill, approve or archive.

Mounted inline above the input by /proposals (one card per pending proposal,
walked in order). Resolves the passed-in Future with "install" | "archive" |
"later". Nothing self-installs: this card IS the trust boundary the
self-evolve design locked — dream drafts, the human decides.

Rows are clickable (house rule) with paired key accelerators: ↑↓ + Enter,
y = install, n = archive, Esc = later (keeps the proposal pending).
"""
from __future__ import annotations

import asyncio

from textual.containers import Vertical
from textual.widgets import Static

from rockycode.palette import LAVENDER, MUTED, VIOLET

_PREVIEW_LINES = 14


class _ChoiceRow(Static):
    def __init__(self, value: str, idx: int, card: "ProposalCard") -> None:
        super().__init__("", id=f"prop-opt-{idx}", classes="prop-opt")
        self._value = value
        self._card = card

    def on_click(self) -> None:
        self._card.pick(self._value)


class ProposalCard(Vertical):
    """Review one proposal. Resolves `future` with the choice."""

    can_focus = True

    BINDINGS = [
        ("up", "move(-1)", "up"),
        ("down", "move(1)", "down"),
        ("enter", "confirm", "select"),
        ("y", "pick('install')", "install"),
        ("n", "pick('archive')", "archive"),
        ("escape", "pick('later')", "later"),
    ]

    DEFAULT_CSS = """
    ProposalCard {
        height: auto;
        margin: 0 1 1 1;
        padding: 1 2;
        background: $surface;
        border: round $primary;
        border-title-color: $text-muted;
        border-subtitle-color: $text-muted;
    }
    ProposalCard #prop-why { color: $text-muted; }
    ProposalCard #prop-preview { margin: 1 0; color: $text-muted; }
    ProposalCard .prop-opt { height: 1; }
    ProposalCard #prop-keys { color: $text-muted; margin-top: 1; }
    """

    def __init__(self, proposal, future: "asyncio.Future[str]") -> None:
        super().__init__()
        self._p = proposal
        self._future = future
        self._idx = 0
        # kind-aware install label: a routine installs to /routines (click-to-run
        # until you grant its lease), a skill to /skills.
        if getattr(proposal, "kind", "skill") == "routine":
            install = "✓  Install routine (global — /routines, click-to-run until you grant its lease)"
        else:
            install = "✓  Install skill (global — /skills next session)"
        self._CHOICES = (
            ("install", install),
            ("archive", "✗  Archive (kept for reference, never asked again)"),
            ("later", "·  Later (stays in the inbox)"),
        )

    def compose(self):
        p = self._p
        ev = ", ".join(p.evidence[:4]) + ("…" if len(p.evidence) > 4 else "")
        yield Static(
            f"[{MUTED}]why: {p.reason or '(recurring pattern)'} · from {ev or 'past sessions'}[/]",
            id="prop-why",
        )
        lines = p.body.splitlines()
        preview = "\n".join(lines[:_PREVIEW_LINES])
        if len(lines) > _PREVIEW_LINES:
            preview += f"\n… (+{len(lines) - _PREVIEW_LINES} more lines)"
        # markup=False: the drafted body is shown literally, never parsed.
        yield Static(preview or "(empty draft)", id="prop-preview", markup=False)
        for i, (value, _label) in enumerate(self._CHOICES):
            yield _ChoiceRow(value, i, self)
        yield Static(
            f"[{LAVENDER}]↑↓[/] choose · [{LAVENDER}]↵[/] select · "
            f"[{LAVENDER}]y[/] install · [{LAVENDER}]n[/] archive · "
            f"[{LAVENDER}]esc[/] later · or just click a row",
            id="prop-keys",
        )

    def on_mount(self) -> None:
        self.border_title = f"🌙 dream proposal · {self._p.name}"
        self.border_subtitle = "drafted while dreaming — nothing installs without you"
        self._render_choices()
        self.focus()

    def _render_choices(self) -> None:
        for i, (_v, label) in enumerate(self._CHOICES):
            row = self.query_one(f"#prop-opt-{i}", Static)
            row.update(f"[b {VIOLET}]▸ {label}[/]" if i == self._idx else f"[{MUTED}]  {label}[/]")

    def action_move(self, delta: int) -> None:
        self._idx = (self._idx + delta) % len(self._CHOICES)
        self._render_choices()

    def action_confirm(self) -> None:
        self.pick(self._CHOICES[self._idx][0])

    def action_pick(self, choice: str) -> None:
        self.pick(choice)

    def pick(self, value: str) -> None:
        """Idempotent — a stray second click/key must not crash the Future."""
        if not self._future.done():
            self._future.set_result(value)
