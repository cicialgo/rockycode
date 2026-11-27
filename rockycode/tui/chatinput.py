"""The chat input: multi-line, history, slash-command completion."""
from __future__ import annotations

import os

from textual import events, on
from textual.message import Message
from textual.widgets import Static, TextArea

from rockycode.engine.redact import redact_typed
from rockycode.i18n import tr

from rockycode.tui import cardkeys, commands
from rockycode.tui.prompt_history import PromptHistory


class ChatInput(TextArea):
    """Multi-line chat input. Enter sends; Ctrl+J inserts a newline; paste keeps
    its newlines. Grows with content up to MAX_LINES, then scrolls — so a
    pasted error or a few lines of code are actually visible while you type.

    (Shift+Enter would be the usual newline key, but the kitty keyboard
    protocol is disabled for IME support, so it's indistinguishable from
    Enter — Ctrl+J is the reliable alternative.)
    """

    MAX_LINES = 8


    class Submitted(Message):
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    class PasteRequested(Message):
        """Ctrl+V in the input: ask the app to read the OS clipboard — an image
        becomes an attachment chip, text just pastes. (The terminal never
        forwards image bytes itself, so this has to go around it.)"""

    def __init__(self, *, history: PromptHistory | None = None, **kwargs) -> None:
        super().__init__(soft_wrap=True, show_line_numbers=False, **kwargs)
        # None → in-memory only (every direct construction in tests stays
        # hermetic); the CLI injects a store so history survives restarts.
        self._store = history
        self._history: list[str] = list(history.items) if history else []
        self._hist_idx: int | None = None
        self._draft = ""                # the in-progress text before browsing history
        self._suggest_widget: Static | None = None

    def on_mount(self) -> None:
        self._sync_height()

    def on_focus(self) -> None:
        cardkeys.refresh_lines(self.app)  # Enter leads from here to the newest card: its line says so

    def on_blur(self) -> None:
        cardkeys.refresh_lines(self.app)  # … and from a picker or a line of the transcript it does not

    def _sync_height(self) -> None:
        # Count VISUAL rows (soft-wrapped), not just logical newlines, so a
        # single long line or a wrapped CJK/pasted block grows the box to fit
        # instead of staying collapsed to one row. wrapped_document.height
        # reflects the current wrap width; fall back to logical lines before the
        # widget has been laid out (width still 0).
        try:
            lines = self.wrapped_document.height or self.document.line_count
        except Exception:  # noqa: BLE001 — height math must never break typing
            lines = self.document.line_count if self.document else 1
        # +2 for the rounded border (border-box height includes it)
        self.styles.height = min(max(lines, 1), self.MAX_LINES) + 2

    def on_resize(self, _event: events.Resize) -> None:
        # Terminal width changed → text re-wraps → recompute visual rows so the
        # box still fits. TextArea._on_resize still runs its own re-wrap.
        self._sync_height()

    def _recall(self, text: str) -> None:
        self.text = text
        self.move_cursor(self.document.end)

    def _cursor_visual_row(self) -> tuple[int, int]:
        """(row, total) of the cursor among VISUAL rows — soft-wrap aware, so a
        long line that wraps to several rows is counted by what's on screen, not
        by newlines. cursor_location[0] is the *logical* line and would treat a
        wrapped single line as one row (up/down would recall history mid-text)."""
        try:
            wd = self.wrapped_document
            return wd.location_to_offset(self.cursor_location).y, wd.height
        except Exception:  # noqa: BLE001 — cursor math must never break typing
            return self.cursor_location[0], self.document.line_count

    async def _on_key(self, event: events.Key) -> None:
        if event.key in ("up", "down"):
            # An arrow here while a card waits may have been meant for the card, whatever it
            # does in the box: the card will not let Enter pick a row the user did not choose.
            cardkeys.poke_all(self.app)
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            text = self.text.strip()
            if text:
                # What is kept for the up-arrow list (and written to disk) has any
                # pasted key masked; what is submitted is the line as typed.
                kept = redact_typed(text)
                if not self._history or self._history[-1] != kept:
                    self._history.append(kept)
                if self._store is not None:
                    self._store.append(text)
                self._hist_idx = None
                self._draft = ""
                self.post_message(self.Submitted(text))
                self.text = ""
                self._sync_height()
            elif self.text:
                self.text = ""  # only blanks: nothing to send
            else:
                # Nothing to send: Enter goes to the card that waits for an answer, if one
                # does. It only brings the keyboard there; the card hears the next Enter,
                # half a second later (rockycode/tui/cardkeys.py).
                card = cardkeys.waiting(self.app)
                if card is not None:
                    card.take_keyboard()
            return
        if event.key == "escape":
            # Esc is the Esc of the card the user was last on, also from here: after one
            # stray letter it must not turn into "stop the turn". Not for a card they never
            # came to, and not for one whose Esc lets something go ahead (the image card's
            # "send without it"): there Esc stops the turn.
            card = cardkeys.last_on(self.app)
            if card is not None and card.ESC_FROM_BOX:
                event.prevent_default()
                event.stop()
                await card.escape()
                return
        if event.key == "ctrl+j":  # reliable manual newline
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        if event.key == "ctrl+v":  # OS-clipboard paste: image → chip, text → insert
            event.prevent_default()
            event.stop()
            self.post_message(self.PasteRequested())
            return
        # Up/Down recall previous inputs — but only from the top/bottom VISUAL
        # row, so while there's text above/below the cursor they just move a row
        # within it (like an editor). Only past the top row does up reach into
        # history — and the in-progress text is saved as the draft first.
        if event.key in ("up", "down") and not self.text and self._hist_idx is None:
            # An empty box and a card that waits: the arrows do nothing. They are no way to
            # a card, and ↑ does not bring back the last message for an Enter meant for the
            # card to send again (rockycode/tui/cardkeys.py).
            if cardkeys.waiting(self.app) is not None:
                event.prevent_default()
                event.stop()
                return
        vrow, vtotal = self._cursor_visual_row()
        if event.key == "up" and vrow == 0 and self._history:
            event.prevent_default()
            event.stop()
            if self._hist_idx is None:
                self._draft = self.text
                self._hist_idx = len(self._history) - 1
            elif self._hist_idx > 0:
                self._hist_idx -= 1
            self._recall(self._history[self._hist_idx])
            return
        if event.key == "down" and self._hist_idx is not None \
                and vrow == vtotal - 1:
            event.prevent_default()
            event.stop()
            if self._hist_idx < len(self._history) - 1:
                self._hist_idx += 1
                self._recall(self._history[self._hist_idx])
            else:
                self._hist_idx = None
                self._recall(self._draft)
            return
        if event.key == "tab":
            # Tab never wanders off: focus used to jump to the transcript and
            # everything typed after it was lost. On a slash command it
            # completes — the one match, or as far as the matches agree. Else it
            # goes to the card that waits for an answer, if one does (as Enter
            # in an empty box does); typing there comes straight back here
            # (rockycode/tui/cardkeys.py).
            event.prevent_default()
            event.stop()
            text = self.text.strip()
            card = None if text.startswith("/") else cardkeys.waiting(self.app)
            if card is not None:
                card.take_keyboard()
            elif text.startswith("/") and "\n" not in text:
                prefix = text.lower()
                matches = [c for c in commands.names(self.app) if c.startswith(prefix)]
                if len(matches) == 1:
                    self.text = matches[0] + " "
                    self.move_cursor(self.document.end)
                    self._hide_suggestions()
                elif matches:
                    common = os.path.commonprefix(matches)
                    if len(common) > len(prefix):
                        self.text = common
                        self.move_cursor(self.document.end)
            return
        await super()._on_key(event)

    @on(TextArea.Changed)
    def _on_changed(self, _event: TextArea.Changed) -> None:
        self._sync_height()
        text = self.text.strip()
        if text.startswith("/") and "\n" not in text:  # a bare "/" lists the commands
            prefix = text.lower()
            matches = [c for c in commands.names(self.app) if c.startswith(prefix)]
            if matches:
                self._show_suggestions(matches, prefix)
            else:
                self._show_suggestions([tr('app.no_match_help_for_all_commands', getattr(self.app, "_language", "en"))], "")
        else:
            self._hide_suggestions()

    def _show_suggestions(self, matches: list[str], prefix: str) -> None:
        try:
            hint = self.app.query_one("#hints", Static)
        except Exception:  # noqa: BLE001
            return
        line = "  ".join(f"[bold]{m}[/]" if m.startswith(prefix) else m for m in matches[:8])
        if len(matches) > 8:
            line += tr('app.more_commands', getattr(self.app, "_language", "en"), count=len(matches) - 8)
        hint.update(line)  # every keystroke: the row used to freeze on its first content
        self._suggest_widget = hint

    def _hide_suggestions(self) -> None:
        if self._suggest_widget is not None:
            try:
                self.app.query_one("#hints", Static).update(
                    tr('app.hints_bar', getattr(self.app, "_language", "en")))
            except Exception:  # noqa: BLE001
                pass
            self._suggest_widget = None

    # Some terminals (iTerm2) route the wheel to the focused input instead of
    # the widget under the pointer, so the transcript never scrolls. Forward
    # wheel events from the input to the transcript so it scrolls regardless.
    def _scroll_chat(self, dy: int) -> None:
        try:
            self.app.query_one("#transcript").scroll_relative(y=dy, animate=False)
        except Exception:  # noqa: BLE001 — best effort; never break input on scroll
            pass

    def on_mouse_scroll_up(self, event) -> None:
        self._scroll_chat(-3)
        event.stop()

    def on_mouse_scroll_down(self, event) -> None:
        self._scroll_chat(3)
        event.stop()
