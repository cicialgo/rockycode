"""The note strip: this project's tasks as one row of clickable notes.

    ♪ 01 fix login   ♫ 02 write tests ✉1   ♩ 03 docs

It appears only while the project has more than one task, so an ordinary chat
never sees it. A note is the visible, clickable form of `/task open <id>` and
takes the same path: the busy guard, then the ordinary exit flow. Without a
mouse the number works too: `/task open 2`. `✉n` means n messages are waiting
in that task's inbox. One terminal runs one session, so a note never claims
that another task is running.
"""
from __future__ import annotations

from rich.cells import cell_len, set_cell_size
from rich.markup import escape
from textual.containers import Horizontal
from textual.widgets import Static

# Rocky speaks in musical chords — the same four notes cycle in the status line.
NOTES = "♪♫♬♩"
TITLE_CELLS = 18  # a title longer than this is cut, by terminal cells
GAP = 3           # blank cells between two notes
PAD = 3           # the strip's own left and right padding


def shorten(text: str, cells: int = TITLE_CELLS) -> str:
    """One line, at most *cells* wide. Cells, not characters: a CJK title is
    twice as wide as its length says."""
    text = " ".join(text.split())
    if cell_len(text) <= cells:
        return text
    return set_cell_size(text, cells - 1).rstrip() + "…"


def label(row: dict) -> str:
    """`♪ 01 title ✉2` as plain text; the number is the task's place in the
    project's creation order, the note only decoration."""
    number = row["number"]
    text = f"{NOTES[(number - 1) % len(NOTES)]} {number:02d}"
    title = shorten(row["title"] or "")
    if title:
        text += f" {title}"
    if row["waiting"]:
        text += f" ✉{row['waiting']}"
    return text


def fit(rows: list[dict], current: str | None, width: int) -> tuple[list[dict], int]:
    """The most recent tasks that fit in *width* cells, oldest first, and how
    many older ones are left out. The current task always stays."""
    room = width - 2 * PAD
    shown: list[dict] = []
    used = 0
    for row in reversed(rows):
        need = cell_len(label(row)) + (GAP if shown else 0)
        if used + need > room and shown:
            break
        shown.insert(0, row)
        used += need
    hidden = len(rows) - len(shown)
    if hidden:
        more = cell_len(f"+{hidden}") + GAP
        here = next((row for row in rows[:hidden] if row["id"] == current), None)
        if here is not None:  # an older current task takes the oldest shown slot
            used += cell_len(label(here)) - cell_len(label(shown[0]))
            shown[0] = here
        while len(shown) > 1 and used + more > room:
            drop = next(row for row in shown if row["id"] != current)
            used -= cell_len(label(drop)) + GAP
            shown.remove(drop)
            hidden += 1
            more = cell_len(f"+{hidden}") + GAP
    return shown, hidden


class TaskNote(Static):
    """One task. Clicking it is `/task open <id>`; the tooltip spells that out,
    so the command is learnable from the screen."""

    def __init__(self, task_id: str | None, text: str, *, here: bool = False, tip: str = "") -> None:
        super().__init__(text, classes="note here" if here else "note")
        self.task_id = task_id
        self.here = here
        self.tooltip = tip or None

    def on_click(self) -> None:
        if not self.here:
            self.app.open_task_note(self.task_id)


class NoteStrip(Horizontal):
    DEFAULT_CSS = """
    NoteStrip { height: 1; padding: 0 3; display: none; }
    NoteStrip.shown { display: block; }
    NoteStrip .note { width: auto; margin-right: 3; color: $text-muted; }
    NoteStrip .note:hover { color: $secondary; text-style: underline; }
    NoteStrip .note.here { color: $accent; text-style: bold; }
    NoteStrip .note.here:hover { text-style: bold; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._drawn: tuple | None = None

    def show(self, rows: list[dict], current: str | None, t, width: int) -> None:
        """Draw *rows* (this project's tasks, oldest first: id, title, number,
        waiting). Nothing is redrawn while nothing changed."""
        state = (tuple((r["id"], r["title"], r["number"], r["waiting"]) for r in rows), current, width)
        if state == self._drawn:
            return
        self._drawn = state
        self.remove_children()
        if len(rows) < 2:
            self.remove_class("shown")
            return
        shown, hidden = fit(rows, current, width)
        notes = []
        if hidden:
            notes.append(TaskNote(None, f"+{hidden}", tip=t("tasks.strip_more", n=hidden)))
        for row in shown:
            here = row["id"] == current
            tip = t("tasks.strip_here") if here else t("tasks.strip_open", n=f"{row['number']:02d}")
            text = escape(label(row))
            if row["waiting"]:
                text = text.replace(f" ✉{row['waiting']}", f" [b]✉{row['waiting']}[/b]")
                tip += " · " + t("tasks.strip_mail", n=row["waiting"])
            notes.append(TaskNote(row["id"], text, here=here, tip=tip))
        self.mount(*notes)
        self.add_class("shown")
