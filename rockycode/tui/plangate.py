"""The plan-approval gate — inline, at the bottom of the chat.

Mounted (not a modal) when a plan-mode turn ends having CHANGED the plan file —
the file write IS the handoff signal (no update_plan tool, no tagged-text
parsing; see docs/plan-mode-design.md). Resolves the passed-in Future with
"approve" | "discuss" | "exit" | "dismiss", then the caller removes it.

Inline (like InlineApproval) so the transcript stays visible and scrollable
while you weigh the plan. Verbs follow the goal-mode gate you already know:
y = approve & build, e = discuss/edit, n = leave plan mode; Esc keeps planning.
"""
from __future__ import annotations

import asyncio

from textual.containers import Vertical
from textual.widgets import Static

from rockycode.palette import LAVENDER, MUTED, VIOLET

_PREVIEW_LINES = 14


class InlinePlanGate(Vertical):
    """Approve a drafted plan. Resolves `future` with the choice; choices are
    plain Static rows (not an OptionList — it swallows pgup/pgdn, and the
    transcript-scroll keys must keep working while this holds focus)."""

    can_focus = True

    BINDINGS = [
        ("up", "move(-1)", "up"),
        ("down", "move(1)", "down"),
        ("enter", "confirm", "select"),
        ("y", "pick('approve')", "approve"),
        ("g", "pick('goal')", "run as goal"),
        ("e", "pick('discuss')", "discuss"),
        ("n", "pick('exit')", "leave"),
        ("escape", "pick('dismiss')", "keep planning"),
    ]

    DEFAULT_CSS = """
    InlinePlanGate {
        height: auto;
        margin: 0 1 1 1;
        padding: 1 2;
        background: $surface;
        border: round $primary;
        border-title-color: $text-muted;
        border-subtitle-color: $text-muted;
    }
    InlinePlanGate #plan-preview { margin: 1 0; color: $text-muted; }
    InlinePlanGate .plan-opt { height: 1; }
    InlinePlanGate #plan-keys { color: $text-muted; margin-top: 1; }
    """

    _CHOICES = (
        ("approve", "▶  Approve & build here"),
        ("goal", "◆  Run as goal (sandbox, autonomous)"),
        ("discuss", "✎  Discuss / edit the plan"),
        ("exit", "✗  Leave plan mode (keep the file)"),
    )

    def __init__(self, rel_path: str, plan_text: str, future: "asyncio.Future[str]") -> None:
        super().__init__()
        self._rel = rel_path
        self._plan = plan_text
        self._future = future
        self._idx = 0

    def compose(self):
        lines = self._plan.splitlines()
        preview = "\n".join(lines[:_PREVIEW_LINES])
        if len(lines) > _PREVIEW_LINES:
            preview += f"\n… (+{len(lines) - _PREVIEW_LINES} more lines in the file)"
        # markup=False: the plan text is shown literally, never parsed.
        yield Static(preview or "(empty plan)", id="plan-preview", markup=False)
        for i in range(len(self._CHOICES)):
            yield Static("", id=f"plan-opt-{i}", classes="plan-opt")
        yield Static(
            f"[{LAVENDER}]↑↓[/] choose   [{LAVENDER}]↵[/] select   "
            f"[{LAVENDER}]y[/] build   [{LAVENDER}]g[/] goal   [{LAVENDER}]e[/] discuss   "
            f"[{LAVENDER}]n[/] leave   [{LAVENDER}]esc[/] keep planning   [dim]· pgup/pgdn scroll[/]",
            id="plan-keys",
        )

    def on_mount(self) -> None:
        self.border_title = f"📋 review the plan · {self._rel}"
        self.border_subtitle = "read-only until you approve"
        self._render_choices()
        self.focus()

    def _render_choices(self) -> None:
        for i, (_v, label) in enumerate(self._CHOICES):
            row = self.query_one(f"#plan-opt-{i}", Static)
            row.update(f"[b {VIOLET}]▸ {label}[/]" if i == self._idx else f"[{MUTED}]  {label}[/]")

    def action_move(self, delta: int) -> None:
        self._idx = (self._idx + delta) % len(self._CHOICES)
        self._render_choices()

    def action_confirm(self) -> None:
        self._resolve(self._CHOICES[self._idx][0])

    def action_pick(self, choice: str) -> None:
        self._resolve(choice)

    def _resolve(self, value: str) -> None:
        if not self._future.done():
            self._future.set_result(value)
