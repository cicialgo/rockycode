"""The inline tool-approval prompt + the turn-cancel signal.

InlineApproval asks the user to approve one tool call and resolves a Future with
one of "once" | "session" | "deny" | "cancel". Unlike a modal it is mounted
*inside* the chat, docked just above the input, so the transcript stays fully
visible and scrollable (wheel / pageup / shift+↑↓) while a tool waits — you can
scroll up to re-read the history that led here, then come back and decide.

↑↓ move the highlight, Enter selects it. The default is run once; a call that
carries a warning starts on Deny instead. The card never takes the keyboard by
itself: the user comes to it (Enter in an empty message box, Tab, a click), so
no key pressed for something else answers it. Every choice is also a row you can
click. No letter answers the card: it opens while the user may be typing, and
what they type is their message (rockycode/tui/cardkeys.py has the whole rule).
Esc maps to "cancel" (abort the
whole turn) — the app's approver turns that into CancelTurn, which propagates out
of Engine.run_turn so its tool-loop finally can keep history valid. The
transcript-scroll keys live on the App as priority bindings, so they keep working
even while this prompt holds focus.

The same card decides a parked step (a call only the user can allow, which yolo
refused instead of waiting): the user opened it, no turn waits behind it, so its
rows are run it now · don't run it · decide later, and Esc means later.

Styling matches the app: explicit hex from the palette, soft purple, minimal.
The OptionList pattern mirrors the resume picker (rockycode/tui/resume.py).
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from rich.markup import escape
from textual.containers import Vertical
from textual.widgets import Static

from rockycode.engine.approval import CancelTurn  # noqa: F401 — re-exported: the TUI's turn driver catches it
from rockycode.i18n import tr, resolve_locale
from rockycode.palette import LAVENDER, MUTED, RED
from rockycode.tui.cardkeys import CardKeys


# Only known engine advisories are translated; custom warnings remain literal.
_WARNING_KEYS = {
    'decodes base64 and runs it': 'permission.warning.base64',
    'edits cron jobs': 'permission.warning.cron',
    'evals a command-substitution result': 'permission.warning.eval_substitution',
    'inline interpreter script that pulls from the network': 'permission.warning.inline_network',
    'looks like a reverse shell': 'permission.warning.reverse_shell',
    'makes a /tmp file executable': 'permission.warning.tmp_executable',
    'pipes a download straight into a shell': 'permission.warning.download_shell',
    'pipes output into a shell': 'permission.warning.pipe_shell',
    'process-substitutes a network download into a command': 'permission.warning.network_substitution',
    'recursive delete of a home/root path': 'permission.warning.recursive_delete',
    'runs a command-substitution result in a shell': 'permission.warning.shell_substitution',
    'writes to a shell/ssh dotfile': 'permission.warning.shell_config',
    "names rocky's key file, a settings file, or its whole folder": 'permission.warning.rocky_store',
    'reads a saved password from the OS keychain': 'permission.warning.keychain_read',
    "this provider's requests, and the key saved for it, will go to this address": 'permission.warning.key_address',
}


class _ChoiceRow(Static):
    """One choice of the card. Clicking it picks it: no choice is reachable by key alone."""

    def __init__(self, value: str, idx: int, card: "InlineApproval") -> None:
        super().__init__("", id=f"perm-opt-{idx}", classes="perm-opt")
        self._value = value
        self._card = card

    def on_click(self) -> None:
        self._card._resolve(self._value)


class InlineApproval(CardKeys, Vertical):
    """Approve one tool call, rendered INLINE at the bottom of the chat instead
    of a modal. Resolves `future` with 'once' | 'session' | 'deny' | 'cancel'
    (a parked step: 'once' | 'deny' | 'later').

    Enter selects the highlighted choice, ↑↓ move it, a click picks a row, Esc
    cancels the whole turn. The highlight starts on run once, or on Deny when the
    call carries a warning. The card waits for the user to come to it.

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
        ("home", "scroll_home", "approval start"),
        ("end", "scroll_end", "approval end"),
    ]

    DEFAULT_CSS = """
    InlineApproval {
        height: auto;
        max-height: 50vh;
        overflow-y: auto;
        scrollbar-size-vertical: 1;
        margin: 0 1;
        padding: 0 1;
        background: $surface;
        border: round $panel-lighten-2;
        border-title-color: $text-muted;
        border-subtitle-color: $text-muted;
    }
    InlineApproval:focus { border: round $primary; }
    InlineApproval #perm-cmd { margin: 0 0 1 0; color: $text; }
    InlineApproval #perm-warn { margin-bottom: 0; }
    InlineApproval .perm-opt { height: auto; min-height: 1; text-opacity: 55%; }
    InlineApproval:focus .perm-opt { text-opacity: 100%; }
    InlineApproval #perm-keys { color: $text-muted; margin-top: 0; }
    """

    KEYS_ID = "#perm-keys"

    def __init__(
        self,
        tool: str,
        detail: str,
        risk: str,
        warning: Optional[str],
        future: "asyncio.Future[str]",
        session_label: Optional[str] = None,
        *, language: str = "en",
        parked_at: Optional[float] = None,   # when yolo parked this step; None = an ordinary approval
        invited: bool = False,               # the user asked for this card (/loop allow): it has the keyboard at once
    ) -> None:
        super().__init__()
        self._parked_at = parked_at
        self._asked_for = invited or parked_at is not None
        self._language = resolve_locale(language)
        self._tool = tool
        self._detail = detail or tr("permission.no_arguments", self._language)
        self._risk = risk
        self._warning = tr(_WARNING_KEYS[warning], self._language) if warning in _WARNING_KEYS else warning
        self._future = future
        # The session grant is offered ONLY when session_label is given. A
        # DANGEROUS bash command passes None → no "allow for session" at all
        # (you can never blanket-grant a risky command); it's run-once or deny.
        self._choices = [("once", tr("permission.once", self._language))]
        if session_label:
            self._choices.append(("session", f"✓  {session_label}"))
        self._choices.append(("deny", tr("permission.deny", self._language)))
        self._idx = 0  # default = run once → Enter approves
        if self._warning:
            self._idx = len(self._choices) - 1  # a warned call starts on Deny: Enter alone never runs it
        if parked_at is not None:
            self._choices = [("once", tr("parked.run", self._language)), ("deny", tr("parked.skip", self._language)),
                             ("later", tr("parked.later", self._language))]
            self._idx = 2  # nothing waits behind this card, so a stray Enter decides nothing

    def compose(self):
        # markup=False: the raw command/url is shown literally, never parsed.
        yield Static(self._detail, id="perm-cmd", markup=False)
        if self._warning:
            yield Static(f"[bold {RED}]⚠ {escape(self._warning)}[/]", id="perm-warn")
        for i, (value, _text) in enumerate(self._choices):
            yield _ChoiceRow(value, i, self)
        yield Static(self._keys_markup(), id="perm-keys")

    def _keys_markup(self) -> str:
        keys = "permission.keys" if self._parked_at is None else "parked.keys"
        return tr(keys, self._language, colour=LAVENDER)

    def _invited(self) -> bool:
        return self._asked_for  # a parked step or /loop allow: the user opened it; a turn's approval opens by itself

    def on_mount(self) -> None:
        risk = self._risk
        if risk.lower() in ("safe", "normal", "low", "medium", "high", "dangerous", "moderate", "risky"):
            risk = tr("permission.risk." + risk.lower(), self._language)
        if self._parked_at is None:
            self.border_title = tr("permission.title", self._language, tool=escape(self._tool))
            self.border_subtitle = tr("permission.risk_label", self._language, risk=escape(risk))
        else:
            self.border_title = tr("parked.title", self._language, tool=escape(self._tool))
            self.border_subtitle = tr("parked.asked_at", self._language,
                                      when=time.strftime("%H:%M", time.localtime(self._parked_at)))
        self._render_choices()

    def _render_choices(self) -> None:
        for i, (_value, text) in enumerate(self._choices):
            row = self.query_one(f"#perm-opt-{i}", Static)
            if i == self._lit():
                row.update(f"[b {LAVENDER}]▸ {escape(text)}[/]")
            else:
                row.update(f"[{MUTED}]  {escape(text)}[/]")

    def action_move(self, delta: int) -> None:
        if self._move(delta, len(self._choices)):
            self._render_choices()
        # also when it could not move: under a long command the rows start out of view
        self.query_one(f"#perm-opt-{self._idx}", Static).scroll_visible(animate=False)

    def _confirm(self) -> None:
        self._resolve(self._choices[self._idx][0])

    def action_cancel_turn(self) -> None:
        self._resolve("cancel" if self._parked_at is None else "later")

    def on_blur(self) -> None:
        """A parked card is open only while it holds the keyboard. When the user takes
        it elsewhere (clicks into the input, types, goes to another card), this one
        closes and the step stays parked: it must not sit there offering "esc: decide
        later" while Esc belongs to something else. Leaving the terminal window is
        not that: the card is still there on return."""
        if self._parked_at is not None and self.app.app_focus:
            self._resolve("later")

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
    ("yolo", "🔓  YOLO", "automatically approves tool calls"),
)
MODE_ROWS_ZH = (
    ("careful", "🔒  谨慎", "写入和命令均先询问"),
    ("ask", "🔒  询问", "高风险操作先询问"),
    ("yolo", "🔓  YOLO", "自动批准工具调用"),
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
        margin: 0 1;
        padding: 0 1;
        background: $surface;
        border: round $primary;
        border-title-color: $text-muted;
        border-subtitle-color: $text-muted;
    }
    PermissionPicker:focus { border: round $primary; }
    PermissionPicker .perm-mode { height: auto; min-height: 1; }
    PermissionPicker #perm-mode-keys { color: $text-muted; margin-top: 0; }
    """

    def __init__(self, current: str, future: "asyncio.Future[str]", *, language: str = "en") -> None:
        super().__init__()
        self._current = current
        self._future = future
        self._language = resolve_locale(language)
        self._rows = tuple((value, tr("permission.mode." + value, self._language),
                            tr("permission.blurb." + value, self._language))
                           for value, _, _ in MODE_ROWS)
        self._idx = next((i for i, r in enumerate(self._rows) if r[0] == current), 1)

    def compose(self):
        for i, (value, _label, _blurb) in enumerate(self._rows):
            yield _ModeRow(value, i, self)
        yield Static(tr("permission.mode_keys", self._language, colour=LAVENDER), id="perm-mode-keys")

    def on_mount(self) -> None:
        self.border_title = tr("permission.mode_title", self._language)
        self.border_subtitle = tr("permission.mode_subtitle", self._language)
        self._render_choices()
        self.focus()

    def _render_choices(self) -> None:
        for i, (value, label, blurb) in enumerate(self._rows):
            row = self.query_one(f"#perm-mode-{i}", Static)
            current = tr("permission.now", self._language)
            now = f"   [{MUTED}]({current})[/]" if value == self._current else ""
            text = escape(f"{label} — {blurb}")
            if i == self._idx:
                row.update(f"[b {LAVENDER}]▸ {text}[/]{now}")
            else:
                row.update(f"[{MUTED}]  {text}[/]{now}")

    def action_move(self, delta: int) -> None:
        self._idx = (self._idx + delta) % len(self._rows)
        self._render_choices()

    def action_confirm(self) -> None:
        self.pick(self._rows[self._idx][0])

    def action_keep(self) -> None:
        self.pick("")

    def pick(self, value: str) -> None:
        """Deliver the choice. Idempotent — a stray second key or click after
        the pick must not crash on an already-set Future; the awaiter's finally
        owns removal, so there's no double-remove either."""
        if not self._future.done():
            self._future.set_result(value)
