"""The inline tool-approval prompt + the turn-cancel signal.

InlineApproval asks the user to approve one tool call and resolves a Future with
one of "once" | "session" | "deny" | "cancel". Unlike a modal it is mounted
*inside* the chat, docked just above the input, so the transcript stays fully
visible and scrollable (wheel / pageup / shift+↑↓) while a tool waits — you can
scroll up to re-read the history that led here, then come back and decide.

Keyboard-driven like Codex / Claude Code: ↑↓ move the highlight, Enter selects it
(default = run once, so Enter approves); y/a/n are accelerators; Esc maps to
"cancel" (abort the whole turn) — the app's approver turns that into CancelTurn,
which propagates out of Engine.run_turn so its tool-loop finally can keep history
valid. The transcript-scroll keys live on the App as priority bindings, so they
keep working even while this prompt holds focus.

Styling matches the app: explicit hex from the palette, soft purple, minimal.
The OptionList pattern mirrors the resume picker (rockycode/tui/resume.py).
"""
from __future__ import annotations

import asyncio
from typing import Optional

from rich.markup import escape
from textual.containers import Vertical
from textual.widgets import Static

from rockycode.palette import LAVENDER, MUTED, RED


class CancelTurn(Exception):
    """Raised by the TUI approver when the user chooses 'cancel turn' (Esc) in
    the approval prompt. Propagates out of Engine.run_turn; the tool-loop
    finally backfills tool responses so history stays API-valid."""


class InlineApproval(Vertical):
    """Approve one tool call, rendered INLINE at the bottom of the chat instead
    of a modal. Resolves `future` with 'once' | 'session' | 'deny' | 'cancel'.

    Enter selects the highlighted choice (default run once → Enter approves),
    ↑↓ move it, y/a/n are accelerators, Esc cancels the whole turn.

    The choices are plain Static rows (not an OptionList) *on purpose*: an
    OptionList swallows PageUp/PageDown for its own paging, which would block
    the App's transcript-scroll bindings while this prompt holds focus. Binding
    only the keys we use here lets pgup/pgdn/shift+↑↓ fall through to the App so
    the user can still scroll the history that led to this call, then decide.
    """

    can_focus = True

    BINDINGS = [
        ("up", "move(-1)", "up"),
        ("down", "move(1)", "down"),
        ("enter", "confirm", "select"),
        ("escape", "cancel_turn", "cancel turn"),
        ("y", "pick('once')", "run once"),
        ("a", "pick('session')", "allow"),
        ("n", "pick('deny')", "deny"),
    ]

    DEFAULT_CSS = """
    InlineApproval {
        height: auto;
        margin: 0 1 1 1;
        padding: 1 2;
        background: $surface;
        border: round $primary;
        border-title-color: $text-muted;
        border-subtitle-color: $text-muted;
    }
    InlineApproval:focus { border: round $primary; }
    InlineApproval #perm-cmd { margin: 1 0; color: $text; }
    InlineApproval #perm-warn { margin-bottom: 1; }
    InlineApproval .perm-opt { height: 1; }
    InlineApproval #perm-keys { color: $text-muted; margin-top: 1; }
    """

    def __init__(
        self,
        tool: str,
        detail: str,
        risk: str,
        warning: Optional[str],
        future: "asyncio.Future[str]",
        session_label: Optional[str] = None,
    ) -> None:
        super().__init__()
        self._tool = tool
        self._detail = detail or "(no arguments)"
        self._risk = risk
        self._warning = warning
        self._future = future
        # The session grant is offered ONLY when session_label is given. A
        # DANGEROUS bash command passes None → no "allow for session" at all
        # (you can never blanket-grant a risky command); it's run-once or deny.
        self._choices = [("once", "▶  Run once")]
        if session_label:
            self._choices.append(("session", f"✓  {session_label}"))
        self._choices.append(("deny", "✗  Deny"))
        self._idx = 0  # default = run once → Enter approves

    def compose(self):
        # markup=False: the raw command/url is shown literally, never parsed.
        yield Static(self._detail, id="perm-cmd", markup=False)
        if self._warning:
            yield Static(f"[bold {RED}]⚠ {escape(self._warning)}[/]", id="perm-warn")
        for i in range(len(self._choices)):
            yield Static("", id=f"perm-opt-{i}", classes="perm-opt")
        yield Static(
            f"[{LAVENDER}]↑↓[/] choose   [{LAVENDER}]↵[/] select   "
            f"[{LAVENDER}]y[/]/[{LAVENDER}]a[/]/[{LAVENDER}]n[/] quick   "
            f"[{LAVENDER}]esc[/] cancel turn   "
            f"[{LAVENDER}]pgup[/]/[{LAVENDER}]shift+↑↓[/] scroll history",
            id="perm-keys",
        )

    def on_mount(self) -> None:
        self.border_title = f"approve tool · {self._tool}"
        self.border_subtitle = f"risk: {self._risk}"
        self._render_choices()
        self.focus()

    def _render_choices(self) -> None:
        for i, (_value, text) in enumerate(self._choices):
            row = self.query_one(f"#perm-opt-{i}", Static)
            if i == self._idx:
                row.update(f"[b {LAVENDER}]▸ {text}[/]")
            else:
                row.update(f"[{MUTED}]  {text}[/]")

    def action_move(self, delta: int) -> None:
        self._idx = (self._idx + delta) % len(self._choices)
        self._render_choices()

    def action_confirm(self) -> None:
        self._resolve(self._choices[self._idx][0])

    def action_pick(self, choice: str) -> None:
        # 'a' must no-op when the session option isn't offered (dangerous bash).
        if choice not in {v for v, _ in self._choices}:
            return
        self._resolve(choice)

    def action_cancel_turn(self) -> None:
        self._resolve("cancel")

    def _resolve(self, value: str) -> None:
        """Deliver the choice to the awaiting approver. Idempotent — a stray
        second key (e.g. Enter after y) must not crash on an already-set Future.
        Removal is left to the awaiter's finally so there's no double-remove."""
        if not self._future.done():
            self._future.set_result(value)


# One row per approval mode, tightest first — the same order shift+tab walks
# (engine.permission.CYCLE). The blurbs say what the mode DOES, not what it is
# called, so the picker teaches the modes instead of assuming you know them.
MODE_ROWS = (
    ("careful", "🔒  careful", "every write & command asks first"),
    ("ask", "🔒  ask", "risky actions (bash · web · mcp) ask first"),
    ("yolo", "🔓  yolo", "no prompts — installs, network and git push run for real"),
)


class _ModeRow(Static):
    """One clickable mode row. Clicking picks it — the picker exists so the
    approval mode is never reachable by keystroke alone."""

    def __init__(self, value: str, idx: int, picker: "PermissionPicker") -> None:
        super().__init__("", id=f"perm-mode-{idx}", classes="perm-mode")
        self._value = value
        self._picker = picker

    def on_click(self) -> None:
        self._picker.pick(self._value)


class PermissionPicker(Vertical):
    """Switch the session's approval mode — the visible, clickable twin of the
    shift+tab cycle (opened by clicking the status-bar chip, or bare
    /permission). Resolves `future` with the chosen mode, or "" to keep the
    current one.

    Inline above the input like InlineApproval rather than a modal screen: the
    transcript stays readable while you decide, and the card sits right next to
    the chip that opened it. ↑↓ + Enter, click a row, Esc keeps what you have.
    """

    can_focus = True

    BINDINGS = [
        ("up", "move(-1)", "up"),
        ("down", "move(1)", "down"),
        ("enter", "confirm", "select"),
        ("escape", "keep", "keep"),
    ]

    DEFAULT_CSS = """
    PermissionPicker {
        height: auto;
        margin: 0 1 1 1;
        padding: 1 2;
        background: $surface;
        border: round $primary;
        border-title-color: $text-muted;
        border-subtitle-color: $text-muted;
    }
    PermissionPicker:focus { border: round $primary; }
    PermissionPicker .perm-mode { height: 1; }
    PermissionPicker #perm-mode-keys { color: $text-muted; margin-top: 1; }
    """

    def __init__(self, current: str, future: "asyncio.Future[str]") -> None:
        super().__init__()
        self._current = current
        self._future = future
        self._idx = next((i for i, r in enumerate(MODE_ROWS) if r[0] == current), 1)

    def compose(self):
        for i, (value, _label, _blurb) in enumerate(MODE_ROWS):
            yield _ModeRow(value, i, self)
        yield Static(
            f"[{LAVENDER}]↑↓[/] choose   [{LAVENDER}]↵[/] switch   "
            f"[{LAVENDER}]esc[/] keep   [{LAVENDER}]click[/] a row   "
            f"[dim]shift+tab cycles without opening this[/]",
            id="perm-mode-keys",
        )

    def on_mount(self) -> None:
        self.border_title = "❖ approval mode"
        self.border_subtitle = "this session · /config permission persists"
        self._render_choices()
        self.focus()

    def _render_choices(self) -> None:
        for i, (value, label, blurb) in enumerate(MODE_ROWS):
            row = self.query_one(f"#perm-mode-{i}", Static)
            now = f"   [{MUTED}](now)[/]" if value == self._current else ""
            text = escape(f"{label} — {blurb}")
            if i == self._idx:
                row.update(f"[b {LAVENDER}]▸ {text}[/]{now}")
            else:
                row.update(f"[{MUTED}]  {text}[/]{now}")

    def action_move(self, delta: int) -> None:
        self._idx = (self._idx + delta) % len(MODE_ROWS)
        self._render_choices()

    def action_confirm(self) -> None:
        self.pick(MODE_ROWS[self._idx][0])

    def action_keep(self) -> None:
        self.pick("")

    def pick(self, value: str) -> None:
        """Deliver the choice. Idempotent — a stray second key or click after
        the pick must not crash on an already-set Future; the awaiter's finally
        owns removal, so there's no double-remove either."""
        if not self._future.done():
            self._future.set_result(value)
