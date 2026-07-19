"""The mode picker: bare /research (or /learn with local additions) opens this
— browse the family's modes with a when-to-use preview, so nobody has to
remember type names. Heavy users graduate to `/research <type>`; the entry
card teaches that at the moment of use.

dismiss() value: a Mode to apply, the string "off" to leave the current mode,
or None on cancel.
"""
from __future__ import annotations

from textual import on
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from rich.markup import escape

from rockycode.engine.modes import Mode
from rockycode.palette import LAVENDER, MUTED


class ModePicker(ModalScreen):
    BINDINGS = [
        ("escape", "cancel", "cancel"),
        ("down", "cursor_down", "↓"),
        ("up", "cursor_up", "↑"),
    ]

    DEFAULT_CSS = """
    ModePicker { align: center middle; }
    ModePicker > #picker {
        width: 76%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $surface;
        border: round $primary;
        border-title-color: $text-muted;
    }
    ModePicker OptionList { height: auto; max-height: 12; background: $surface; }
    ModePicker #mode-preview { height: auto; margin-top: 1; color: $text-muted; }
    ModePicker #mode-guide { height: 1; margin-top: 1; color: $text-muted; }
    """

    def __init__(self, family: str, modes: list[Mode], *, active: str | None = None) -> None:
        super().__init__()
        self.family = family
        self.modes = modes
        self.active = active  # currently active mode name (any family) or None

    def compose(self):
        with Vertical(id="picker") as box:
            box.border_title = f"/{self.family} — pick how we work"
            yield OptionList(id="mode-list")
            yield Static("", id="mode-preview")
            yield Static(
                f"[{MUTED}]↑↓ move · ↵ apply · esc cancel · "
                f"direct next time: [{LAVENDER}]/{self.family} <name>[/][/]",
                id="mode-guide",
            )

    def on_mount(self) -> None:
        ol = self.query_one("#mode-list", OptionList)
        if self.active:
            ol.add_option(Option(f"  ◉ off{'':<18} — back to normal rocky (now: {self.active})", id="off"))
        for i, m in enumerate(self.modes):
            marker = "▸ " if m.name == self.active else "  "
            local = "" if m.builtin else "  ·  project-local"
            ol.add_option(Option(f"{marker}{m.name:<22} — {escape(m.description)}{local}", id=str(i)))
        ol.highlighted = 0
        ol.focus()
        self._show_preview(0 if not self.active else None)

    def _show_preview(self, idx: int | None) -> None:
        pv = self.query_one("#mode-preview", Static)
        if idx is None or not (0 <= idx < len(self.modes)):
            pv.update(f"[{MUTED}]leave the current mode — rocky goes back to its plain contract.[/]")
            return
        pv.update(f"[{MUTED}]{escape(self.modes[idx].preview)}[/]")

    @on(OptionList.OptionHighlighted)
    def _on_highlight(self, e: OptionList.OptionHighlighted) -> None:
        oid = e.option.id
        self._show_preview(int(oid) if oid and oid.isdigit() else None)

    @on(OptionList.OptionSelected)
    def _on_select(self, e: OptionList.OptionSelected) -> None:
        oid = e.option.id
        if oid == "off":
            self.dismiss("off")
        elif oid and oid.isdigit():
            self.dismiss(self.modes[int(oid)])

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_cursor_down(self) -> None:
        self.query_one("#mode-list", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#mode-list", OptionList).action_cursor_up()
