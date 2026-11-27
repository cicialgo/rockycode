"""Keyboard-first artifact cards and project/version picker."""
from __future__ import annotations

from datetime import datetime

from rich.markup import escape
from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from rockycode.i18n import tr


class ArtifactAction(Message):
    def __init__(self, action: str, artifact_id: str, revision: int | None = None) -> None:
        super().__init__()
        self.action, self.artifact_id, self.revision = action, artifact_id, revision


class ArtifactCard(Vertical):
    DEFAULT_CSS = """
    ArtifactCard { height: auto; margin: 1 0; padding: 0 1; border-left: solid $primary; }
    ArtifactCard .artifact-card-title { height: auto; text-style: bold; }
    ArtifactCard .artifact-card-meta { height: auto; color: $text-muted; }
    ArtifactCard Horizontal { height: auto; }
    ArtifactCard Button { min-width: 8; width: auto; height: 1; border: none; margin-right: 1; }
    """

    def __init__(self, record, language="en"):
        super().__init__()
        self.record, self.language = record, language

    def compose(self):
        yield Static(escape(self.record.title), classes="artifact-card-title")
        yield Static(self._meta(), classes="artifact-card-meta")
        with Horizontal():
            for action in ('open', 'edit', 'history', 'copy'):
                yield Button(tr('artifact_ui.' + action, self.language), name=action, compact=True)

    def _meta(self):
        return tr('artifact_ui.saved', self.language, revision=self.record.revision)

    def update_record(self, record):
        self.record = record
        self.query_one('.artifact-card-title', Static).update(escape(record.title))
        self.query_one('.artifact-card-meta', Static).update(self._meta())

    @on(Button.Pressed)
    def choose(self, event):
        event.stop()
        self.post_message(ArtifactAction(event.button.name, self.record.artifact_id or self.record.name))


class ArtifactPanel(ModalScreen):
    BINDINGS = [("escape", "close", "close"), ("down", "next", ""), ("up", "previous", "")]
    # The same frame as the session and key pickers: a titled round border, a
    # search field, the list, then one-line buttons.
    DEFAULT_CSS = """
    ArtifactPanel { align: center middle; }
    ArtifactPanel > #picker {
        width: 94%; max-width: 100; height: auto; max-height: 90%;
        padding: 1 2; background: $surface;
        border: round $primary; border-title-color: $text-muted;
    }
    ArtifactPanel #artifact-search { border: round $panel-lighten-2; margin-bottom: 1; }
    ArtifactPanel #artifact-search:focus { border: round $primary; }
    ArtifactPanel #artifact-list { height: auto; min-height: 3; max-height: 12; background: $surface; }
    ArtifactPanel #artifact-detail { height: auto; color: $text-muted; }
    ArtifactPanel Horizontal { height: auto; margin-top: 1; }
    ArtifactPanel Button { width: auto; min-width: 8; height: 1; border: none; margin-right: 1; }
    /* the picker sits on $surface, which is also a button's own background */
    ArtifactPanel Button.-style-default { background: $panel-lighten-2; }
    ArtifactPanel #artifact-guide { height: auto; color: $text-muted; margin-top: 1; }
    """

    def __init__(self, registry, *, language="en", artifact_id=None):
        super().__init__()
        self.registry, self.language, self.artifact_id = registry, language, artifact_id
        self.scope = 'project'
        self.rows = {}
        self._signature = None

    def compose(self):
        with VerticalScroll(id='picker') as box:
            box.border_title = tr('artifact_ui.history_title' if self.artifact_id else 'artifact_ui.title', self.language)
            yield Input(placeholder=tr('artifact_ui.search', self.language), id='artifact-search')
            yield OptionList(id='artifact-list')
            yield Static('', id='artifact-detail', markup=False)
            with Horizontal():
                actions = ('open', 'restore', 'copy') if self.artifact_id else ('open', 'edit', 'history', 'copy', 'live')
                for action in actions:
                    yield Button(tr('artifact_ui.' + action, self.language), id='artifact-' + action, name=action, compact=True)
            if not self.artifact_id:
                with Horizontal():
                    yield Button(tr('artifact_ui.project', self.language), id='artifact-scope', compact=True)
            yield Static(tr('artifact_ui.guide', self.language), id='artifact-guide')

    def on_mount(self):
        self.refresh_rows()
        self.set_interval(1, self.refresh_rows)
        self.query_one('#artifact-search', Input).focus()

    def selected(self):
        ol = self.query_one(OptionList)
        if ol.highlighted is None:
            return None
        return self.rows.get(ol.get_option_at_index(ol.highlighted).id)

    def refresh_rows(self):
        query = self.query_one(Input).value.casefold()
        rows = self.registry.history(self.artifact_id) if self.artifact_id else self.registry.records(self.scope)
        choices = []
        for row in rows:
            if self.artifact_id:
                key, title, revision, timestamp = str(row['revision']), row['title'], row['revision'], row['created']
            else:
                key, title, revision, timestamp = row.artifact_id or row.name, row.title, row.revision, row.updated_at
            if query not in title.casefold() and query not in key.casefold():
                continue
            stamp = datetime.fromtimestamp(timestamp).strftime('%m-%d %H:%M')
            label = f'{escape(title)}  [dim]v{revision} · {stamp}[/]'
            choices.append((key, label, row))
        signature = [(key, label) for key, label, _ in choices]
        if signature == self._signature:
            return
        ol = self.query_one(OptionList)
        selected_id = ol.get_option_at_index(ol.highlighted).id if ol.highlighted is not None else None
        self._signature = signature
        self.rows = {key: row for key, _, row in choices}
        ol.clear_options()
        for key, label, _ in choices:
            ol.add_option(Option(label, id=key))
        if choices:
            ol.highlighted = next((i for i, row in enumerate(choices) if row[0] == selected_id), 0)
        self.show_detail()

    def show_detail(self):
        row = self.selected()
        detail = self.query_one('#artifact-detail', Static)
        detail.update('' if row is not None else tr('artifact_ui.empty', self.language))
        detail.display = row is None
        for button in self.query('Button'):
            if button.id != 'artifact-scope':
                button.disabled = row is None

    @on(Input.Changed)
    def search(self):
        self.refresh_rows()

    @on(Input.Submitted)
    def search_enter(self):
        self.choose('open')

    @on(OptionList.OptionHighlighted)
    def highlight(self):
        self.show_detail()

    @on(OptionList.OptionSelected)
    def activate(self):
        self.choose('open')

    @on(Button.Pressed)
    def button(self, event):
        event.stop()
        if event.button.id == 'artifact-scope':
            self.scope = 'session' if self.scope == 'project' else 'project'
            event.button.label = tr('artifact_ui.' + self.scope, self.language)
            self._signature = None
            self.refresh_rows()
        else:
            self.choose(event.button.name)

    def choose(self, action):
        row = self.selected()
        if row is not None:
            aid = self.artifact_id or row.artifact_id or row.name
            revision = row['revision'] if self.artifact_id else None
            self.dismiss((action, aid, revision))

    def action_next(self):
        self.query_one(OptionList).action_cursor_down()

    def action_previous(self):
        self.query_one(OptionList).action_cursor_up()

    def action_close(self):
        self.dismiss(None)
