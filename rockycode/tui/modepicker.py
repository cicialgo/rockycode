"""The mode picker: bare /research (or /learn with local additions) opens this
— browse the family's modes with a when-to-use preview, so nobody has to
remember type names. Heavy users graduate to `/research <type>`; the entry
card teaches that at the moment of use.

dismiss() value: a Mode to apply, the string "off" to leave the current mode,
or None on cancel.
"""
from __future__ import annotations

from textual import on
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from rich.markup import escape

from rockycode.engine.modes import Mode
from rockycode.i18n import tr
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
    ModePicker #mode-guide { height: auto; margin-top: 1; color: $text-muted; }
    """

    def __init__(self, family: str, modes: list[Mode], *, active: str | None = None,
                 language: str = "en") -> None:
        super().__init__()
        self.language = language
        self.family = family
        self.modes = modes
        self.active = active  # currently active mode name (any family) or None

    def compose(self):
        with VerticalScroll(id="picker") as box:
            box.border_title = tr("selectors.mode.title", self.language, family=escape(self.family))
            yield OptionList(id="mode-list")
            yield Static("", id="mode-preview")
            yield Static(
                tr("selectors.mode.guide", self.language, muted=MUTED, accent=LAVENDER, family=escape(self.family)),
                id="mode-guide",
            )

    def on_mount(self) -> None:
        ol = self.query_one("#mode-list", OptionList)
        if self.active:
            ol.add_option(Option(tr("selectors.mode.off", self.language, active=escape(self.active)), id="off"))
        for i, m in enumerate(self.modes):
            marker = "▸ " if m.name == self.active else "  "
            local = "" if m.builtin else tr("selectors.mode.local", self.language)
            ol.add_option(Option(f"{marker}{escape(m.name):<22} — {escape(self._mode_text(m, 'description'))}{local}", id=str(i)))
        ol.highlighted = 0
        ol.focus()
        self._show_preview(0 if not self.active else None)

    def _mode_text(self, mode: Mode, field: str) -> str:
        # Project-local copy is user content, even if its name shadows a builtin.
        key = f"selectors.mode.{mode.name}.{field}"
        translated = tr(key, self.language) if mode.builtin else key
        return getattr(mode, field) if translated == key else translated

    def _show_preview(self, idx: int | None) -> None:
        pv = self.query_one("#mode-preview", Static)
        if idx is None or not (0 <= idx < len(self.modes)):
            pv.update(tr("selectors.mode.leave", self.language, muted=MUTED))
            return
        pv.update(f"[{MUTED}]{escape(self._mode_text(self.modes[idx], 'preview'))}[/]")

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
