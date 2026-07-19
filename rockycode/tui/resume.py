"""The resume picker: a modal list of past sessions, newest on top.

Default scope is the current folder; Ctrl+A flips to all folders (the global
registry). Type to search; ↑↓ move; Enter resumes; Esc cancels. The search
box stays focused so you can filter immediately — arrows/enter are handled at
the screen level so they work without leaving the input.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from textual import on
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from rich.markup import escape

from rockycode.palette import LAVENDER, MUTED, VIOLET
from rockycode.session import SessionInfo, list_sessions, public_id


def rel_time(ts: float) -> str:
    d = time.time() - ts
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    if d < 172800:
        return "yesterday"
    return f"{int(d // 86400)}d ago"


class ResumePicker(ModalScreen):
    """Returns the chosen SessionInfo via dismiss(), or None on cancel."""

    BINDINGS = [
        ("escape", "cancel", "cancel"),
        ("down", "cursor_down", "↓"),
        ("up", "cursor_up", "↑"),
        # priority: the search Input otherwise eats ctrl+a as "line start"
        Binding("ctrl+a", "toggle_scope", "all folders", priority=True),
    ]

    DEFAULT_CSS = """
    ResumePicker { align: center middle; }
    ResumePicker > #picker {
        width: 88%;
        height: 80%;
        padding: 1 2;
        background: $surface;
        border: round $primary;
        border-title-color: $text-muted;
    }
    ResumePicker #q { border: round $panel-lighten-2; margin-bottom: 1; }
    ResumePicker #q:focus { border: round $primary; }
    ResumePicker OptionList { height: 1fr; background: $surface; }
    ResumePicker #picker-guide { height: 1; color: $text-muted; }
    """

    def __init__(self, workdir: Path) -> None:
        super().__init__()
        self.workdir = workdir
        self.scope = "project"
        self._sessions: list[SessionInfo] = []

    def compose(self):
        with Vertical(id="picker") as box:
            box.border_title = "resume a session"
            yield Input(placeholder="type to search…", id="q")
            yield OptionList(id="list")
            yield Static("", id="picker-guide")

    def on_mount(self) -> None:
        self.query_one("#q", Input).focus()
        self._reload()

    def _guide(self) -> str:
        where = "this folder" if self.scope == "project" else "all folders"
        return (
            f"[{MUTED}]scope: [{LAVENDER}]{where}[/]  ·  ↑↓ move · ↵ resume · "
            f"^a {'this folder' if self.scope == 'all' else 'all folders'} · esc cancel[/]"
        )

    def _fmt(self, s: SessionInfo) -> str:
        when = rel_time(s.started_at)
        folder = Path(s.project_path).name if s.project_path else s.project_name
        # escape: a past session's title/summary is untrusted user text; a stray
        # "[/…]" in it is a Textual markup close tag that would crash the picker.
        title = escape(s.display_title)
        return (f"{when:>9}  {title[:44]:<44}  {s.n_messages:>3} msgs  "
                f"{public_id(s.session_id)}  📁 {folder}")

    def _reload(self) -> None:
        q = self.query_one("#q", Input).value or None
        self._sessions = list_sessions(self.scope, workdir=self.workdir, query=q)
        ol = self.query_one("#list", OptionList)
        ol.clear_options()
        if self._sessions:
            for i, s in enumerate(self._sessions):
                ol.add_option(Option(self._fmt(s), id=str(i)))
            ol.highlighted = 0
        else:
            ol.add_option(Option("(no sessions found)", id="none", disabled=True))
        self.query_one("#picker-guide", Static).update(self._guide())

    @on(Input.Changed, "#q")
    def _on_search(self, _e: Input.Changed) -> None:
        self._reload()

    @on(Input.Submitted, "#q")
    def _on_enter(self, _e: Input.Submitted) -> None:
        self._select_current()

    @on(OptionList.OptionSelected)
    def _on_click(self, e: OptionList.OptionSelected) -> None:
        if e.option.id and e.option.id.isdigit():
            self.dismiss(self._sessions[int(e.option.id)])

    def _select_current(self) -> None:
        ol = self.query_one("#list", OptionList)
        if ol.highlighted is not None and self._sessions:
            self.dismiss(self._sessions[ol.highlighted])

    def action_cursor_down(self) -> None:
        self.query_one("#list", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#list", OptionList).action_cursor_up()

    def action_toggle_scope(self) -> None:
        self.scope = "all" if self.scope == "project" else "project"
        self._reload()

    def action_cancel(self) -> None:
        self.dismiss(None)
