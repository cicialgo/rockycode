"""Small shared widgets of the shell."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from rockycode.i18n import tr



class TextViewerScreen(ModalScreen):
    """Scrollable full-screen viewer for long text (system prompt, files).

    A modal owns keyboard focus, so arrows/PageUp/PageDown scroll naturally —
    in the transcript those keys belong to the Input.
    """

    BINDINGS = [("escape", "close", "close"), ("q", "close", "close")]

    DEFAULT_CSS = """
    TextViewerScreen { align: center middle; }
    TextViewerScreen > VerticalScroll {
        width: 90%;
        height: 90%;
        padding: 1 2;
        background: $surface;
        border: round $primary;
        border-title-color: $text-muted;
        border-subtitle-color: $text-muted;
    }
    """

    def __init__(self, title: str, text: str) -> None:
        super().__init__()
        self._title = title
        self._text = text

    def compose(self) -> ComposeResult:
        body = VerticalScroll(Static(self._text))
        body.border_title = self._title
        body.border_subtitle = tr('app.pgup_pgdn_scroll_esc_close', getattr(self.app, "_language", "en"))
        yield body

    def on_mount(self) -> None:
        self.query_one(VerticalScroll).focus()

    def action_close(self) -> None:
        self.dismiss()


class PermChip(Static):
    """The status-bar approval badge — and the clickable way to change it.

    rockycode never ships an action reachable only by a keystroke, so this chip
    does three jobs at once: it says which mode is live, it spells out the
    shift+tab shortcut so the key is discoverable from the screen itself, and
    clicking it opens the full picker."""

    def on_click(self) -> None:
        self.app.open_permission_picker()


class ParkChip(Static):
    """The status-bar count of steps only the user can allow, which yolo refused
    instead of waiting — and the clickable way to decide them. Hidden when
    nothing waits."""

    def on_click(self) -> None:
        self.app.open_parked()


class EditChip(Static):
    """The status-bar note that the next message edits one artifact — and the
    clickable way out of that. Hidden unless an artifact is selected for editing."""

    def on_click(self) -> None:
        self.app._cancel_artifact_edit()
