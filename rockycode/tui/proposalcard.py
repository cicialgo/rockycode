"""The proposal review card — one dream-drafted skill, approve or archive.

Mounted inline above the input by /proposals (one card per pending proposal,
walked in order). Resolves the passed-in Future with "install" | "archive" |
"later". Nothing self-installs: this card IS the trust boundary the
self-evolve design locked — dream drafts, the human decides.

Rows are clickable (house rule); ↑↓ + Enter picks one, Esc = later (keeps
the proposal pending). No letter answers it: what the user types is their
message (rockycode/tui/cardkeys.py).
"""
from __future__ import annotations

import asyncio

from rich.markup import escape
from textual.containers import Vertical
from textual.widgets import Static

from rockycode.i18n import tr, resolve_locale
from rockycode.palette import LAVENDER, MUTED, VIOLET
from rockycode.tui.cardkeys import CardKeys

_PREVIEW_LINES = 14


class _ChoiceRow(Static):
    def __init__(self, value: str, idx: int, card: "ProposalCard") -> None:
        super().__init__("", id=f"prop-opt-{idx}", classes="prop-opt")
        self._value = value
        self._card = card

    def on_click(self) -> None:
        self._card.pick(self._value)


class ProposalCard(CardKeys, Vertical):
    """Review one proposal. Resolves `future` with the choice."""

    can_focus = True

    BINDINGS = [
        ("up", "move(-1)", "up"),
        ("down", "move(1)", "down"),
        ("enter", "confirm", "select"),
        ("escape", "pick('later')", "later"),
    ]

    DEFAULT_CSS = """
    ProposalCard {
        height: auto;
        margin: 0 1 1 1;
        padding: 1 2;
        background: $surface;
        border: round $panel-lighten-2;
        border-title-color: $text-muted;
        border-subtitle-color: $text-muted;
    }
    ProposalCard:focus { border: round $primary; }
    ProposalCard #prop-why { color: $text-muted; }
    ProposalCard #prop-tested { height: 1; }
    ProposalCard #prop-preview { margin: 1 0; color: $text-muted; }
    ProposalCard .prop-opt { height: auto; min-height: 1; text-opacity: 55%; }
    ProposalCard:focus .prop-opt { text-opacity: 100%; }
    ProposalCard #prop-keys { color: $text-muted; margin-top: 1; }
    """

    KEYS_ID = "#prop-keys"

    def __init__(self, proposal, future: "asyncio.Future[str]", *, language: str = "en") -> None:
        self._language = resolve_locale(language)
        super().__init__()
        self._p = proposal
        self._future = future
        self._idx = 0
        # kind-aware install label: a routine installs to /routines (click-to-run
        # until you grant its lease), a skill to /skills, a retire moves the
        # skill aside (reversible — .archived, never deleted).
        kind = getattr(proposal, "kind", "skill")
        if kind == "routine":
            install = tr("widgets.proposal.install_routine", self._language)
        elif kind == "retire-skill":
            install = tr("widgets.proposal.install_retire", self._language)
        else:
            install = tr("widgets.proposal.install_skill", self._language)
        self._CHOICES = (
            ("install", install),
            ("archive", tr("widgets.proposal.archive", self._language)),
            ("later", tr("widgets.proposal.later", self._language)),
        )

    def compose(self):
        p = self._p
        ev = ", ".join(p.evidence[:4]) + ("…" if len(p.evidence) > 4 else "")
        yield Static(
            tr("widgets.proposal.why", self._language, colour=MUTED, reason=escape(p.reason or tr("widgets.proposal.pattern", self._language)), evidence=escape(ev or tr("widgets.proposal.past", self._language))),
            id="prop-why",
        )
        # The dry-run receipt (routines): the ask carries its own proof.
        tested = getattr(p, "tested", "")
        if tested == "ok":
            yield Static(tr("widgets.proposal.tested_ok", self._language, colour=LAVENDER, muted=MUTED,
                            note=escape(p.test_note)), id="prop-tested")
        elif tested == "unavailable":
            yield Static(tr("widgets.proposal.untested", self._language, muted=MUTED,
                            note=escape(p.test_note or tr("widgets.proposal.untested_default", self._language))),
                         id="prop-tested")
        lines = p.body.splitlines()
        preview = "\n".join(lines[:_PREVIEW_LINES])
        if len(lines) > _PREVIEW_LINES:
            preview += tr("widgets.proposal.more", self._language, count=len(lines) - _PREVIEW_LINES)
        # markup=False: the drafted body is shown literally, never parsed.
        yield Static(preview or tr("widgets.proposal.empty", self._language), id="prop-preview", markup=False)
        for i, (value, _label) in enumerate(self._CHOICES):
            yield _ChoiceRow(value, i, self)
        yield Static(self._keys_markup(), id="prop-keys")

    def _keys_markup(self) -> str:
        return tr("widgets.proposal.keys", self._language, colour=LAVENDER)

    def on_mount(self) -> None:
        self.border_title = tr("widgets.proposal.title", self._language, name=escape(self._p.name))
        self.border_subtitle = tr("widgets.proposal.subtitle", self._language)
        self._render_choices()

    def _render_choices(self) -> None:
        for i, (_v, label) in enumerate(self._CHOICES):
            row = self.query_one(f"#prop-opt-{i}", Static)
            row.update(f"[b {VIOLET}]▸ {label}[/]" if i == self._lit() else f"[{MUTED}]  {label}[/]")

    def action_move(self, delta: int) -> None:
        if self._move(delta, len(self._CHOICES)):
            self._render_choices()

    def _confirm(self) -> None:
        self.pick(self._CHOICES[self._idx][0])

    def action_pick(self, choice: str) -> None:
        self.pick(choice)

    def pick(self, value: str) -> None:
        """Idempotent — a stray second click/key must not crash the Future."""
        if not self._future.done():
            self._future.set_result(value)
