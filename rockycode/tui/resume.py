"""The resume picker: a modal list of past sessions, newest on top.

Default scope is the current folder; Ctrl+A flips to all folders (the global
registry). Type to search; ↑↓ move; Enter resumes; Esc cancels. The search
box stays focused so you can filter immediately — arrows/enter are handled at
the screen level so they work without leaving the input.
"""
from __future__ import annotations

import time
from pathlib import Path

from textual import on
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from rich.cells import cell_len, set_cell_size
from rich.markup import escape

from rockycode.i18n import tr
from rockycode.palette import LAVENDER, MUTED
from rockycode.session import SessionInfo, list_sessions, public_id


def rel_time(ts: float, language: str = "en") -> str:
    d = time.time() - ts
    if d < 60:
        return tr("selectors.time.now", language)
    if d < 3600:
        return tr("selectors.time.minutes", language, count=int(d // 60))
    if d < 86400:
        return tr("selectors.time.hours", language, count=int(d // 3600))
    if d < 172800:
        return tr("selectors.time.yesterday", language)
    return tr("selectors.time.days", language, count=int(d // 86400))


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
    ResumePicker #picker-guide { height: auto; color: $text-muted; }
    """

    def __init__(self, workdir: Path, *, language: str = "en") -> None:
        super().__init__()
        self.language = language
        self.workdir = workdir
        self.scope = "project"
        self._sessions: list[SessionInfo] = []

    def compose(self):
        with Vertical(id="picker") as box:
            box.border_title = tr("selectors.resume.title", self.language)
            yield Input(placeholder=tr("selectors.resume.search", self.language), id="q")
            yield OptionList(id="list")
            yield Static("", id="picker-guide")

    def on_mount(self) -> None:
        self.query_one("#q", Input).focus()
        self._reload()

    def _guide(self) -> str:
        where = tr("selectors.resume." + self.scope, self.language)
        other = tr("selectors.resume." + ("project" if self.scope == "all" else "all"), self.language)
        return tr("selectors.resume.guide", self.language, muted=MUTED,
                  accent=LAVENDER, where=where, other=other)

    def _fmt(self, s: SessionInfo) -> str:
        when = rel_time(s.started_at, self.language)
        folder = Path(s.project_path).name if s.project_path else s.project_name
        # escape: a past session's title/summary is untrusted user text; a stray
        # "[/…]" in it is a Textual markup close tag that would crash the picker.
        # Size raw text in terminal cells BEFORE escaping markup. Slicing an
        # escaped title can split its backslash/tag and CJK is two cells wide.
        raw_title = s.display_title
        title = escape(set_cell_size(raw_title, 44) if cell_len(raw_title) != 44 else raw_title)
        return tr("selectors.resume.row", self.language,
                  when=" " * max(0, 9 - cell_len(when)) + when, title=title, count=s.n_messages,
                  session=escape(public_id(s.session_id)), folder=escape(folder))

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
            ol.add_option(Option(tr("selectors.resume.empty", self.language), id="none", disabled=True))
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
