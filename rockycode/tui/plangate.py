"""The plan-approval gate — inline, at the bottom of the chat.

Mounted after submit_plan or the user's /plan review request. Resolves the Future with
"approve" | "discuss" | "exit" | "dismiss", then the caller removes it.

Inline (like InlineApproval) so the transcript stays visible and scrollable
while you weigh the plan. ↑↓ + Enter or a click on a row picks; Esc keeps
planning. It opens when the model submits the plan, so no letter answers it:
what the user types is their message (rockycode/tui/cardkeys.py).
"""
from __future__ import annotations

import asyncio

from rich.markup import escape

from textual.containers import Vertical, VerticalScroll
from textual.widgets import Markdown, Static

from rockycode.i18n import tr
from rockycode.palette import LAVENDER, MUTED, VIOLET
from rockycode.tui.cardkeys import CardKeys


class _Row(Static):
    def __init__(self, value: str, idx: int, card: "InlinePlanGate") -> None:
        super().__init__("", id=f"plan-opt-{idx}", classes="plan-opt")
        self._value = value
        self._card = card

    def on_click(self) -> None:
        self._card._resolve(self._value)


class InlinePlanGate(CardKeys, Vertical):
    """Approve a drafted plan. Resolves `future` with the choice; choices are
    plain Static rows (not an OptionList — it swallows pgup/pgdn, and the
    transcript-scroll keys must keep working while this holds focus)."""

    can_focus = True

    BINDINGS = [
        ("up", "move(-1)", "up"),
        ("down", "move(1)", "down"),
        ("enter", "confirm", "select"),
        ("pageup", "scroll_plan(-1)", "scroll plan up"),
        ("pagedown", "scroll_plan(1)", "scroll plan down"),
        ("escape", "pick('dismiss')", "keep planning"),
    ]

    DEFAULT_CSS = """
    InlinePlanGate {
        height: auto;
        margin: 0 1 1 1;
        padding: 1 2;
        background: $surface;
        border: round $panel-lighten-2;
        border-title-color: $text-muted;
        border-subtitle-color: $text-muted;
    }
    InlinePlanGate:focus { border: round $primary; }
    InlinePlanGate #plan-scroll { height: auto; max-height: 45vh; margin: 1 0; }
    InlinePlanGate #plan-preview { height: auto; }
    InlinePlanGate .plan-opt { height: auto; text-opacity: 55%; }
    InlinePlanGate:focus .plan-opt { text-opacity: 100%; }
    InlinePlanGate #plan-keys { color: $text-muted; margin-top: 1; }
    """

    KEYS_ID = "#plan-keys"

    _CHOICES = (
        ("discuss", "✎  Continue discussing the plan"),
        ("approve", "▶  Approve & build here"),
        ("goal", "◆  Run as goal (sandbox, autonomous)"),
        ("exit", "✗  Leave plan mode (keep the file)"),
    )

    def __init__(self, rel_path: str, plan_text: str, future: "asyncio.Future[str]",
                 *, language: str = "en", invited: bool = False) -> None:
        super().__init__()
        self._asked_for = invited  # the user's /plan, or the card coming back after their own choice
        self.language = language
        self._rel = rel_path
        self._plan = plan_text
        self._future = future
        self._idx = 0

    def compose(self):
        # Not focusable: a click in the plan must not take the keyboard off the card
        # (pgup/pgdn and the wheel scroll it).
        with VerticalScroll(id="plan-scroll", can_focus=False):
            yield Markdown(self._plan, id="plan-preview", open_links=False)
        for i, (value, _label) in enumerate(self._CHOICES):
            yield _Row(value, i, self)
        yield Static(self._keys_markup(), id="plan-keys")

    def _keys_markup(self) -> str:
        return tr("selectors.plan.guide", self.language, accent=LAVENDER)

    def _invited(self) -> bool:
        return self._asked_for  # else it opened when the model submitted the plan: it waits

    def on_mount(self) -> None:
        self.border_title = tr("selectors.plan.title", self.language, path=escape(self._rel))
        self.border_subtitle = tr("selectors.plan.subtitle", self.language)
        self._render_choices()

    def _render_choices(self) -> None:
        for i, (value, _label) in enumerate(self._CHOICES):
            label = tr(f"selectors.plan.{value}", self.language)
            row = self.query_one(f"#plan-opt-{i}", Static)
            row.update(f"[b {VIOLET}]▸ {label}[/]" if i == self._lit() else f"[{MUTED}]  {label}[/]")

    def action_move(self, delta: int) -> None:
        if self._move(delta, len(self._CHOICES)):
            self._render_choices()

    def _confirm(self) -> None:
        self._resolve(self._CHOICES[self._idx][0])

    def action_scroll_plan(self, direction: int) -> None:
        preview = self.query_one("#plan-scroll", VerticalScroll)
        if direction < 0:
            preview.scroll_page_up(animate=False)
        else:
            preview.scroll_page_down(animate=False)

    def action_pick(self, choice: str) -> None:
        self._resolve(choice)

    def _resolve(self, value: str) -> None:
        if not self._future.done():
            self._future.set_result(value)
