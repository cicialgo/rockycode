"""The model picker: bare /model opens this — browse your keyed
provider:model choices with ↑↓ or a click, preview the endpoint + key status,
switch on ↵. The "N more" row (or `/model all`) reopens over the full EN/CN
catalog. Heavy users graduate to `/model <spec>`; the guide line teaches that
at the moment of use — same teach-at-use pattern as the mode picker.

dismiss() value: a providers.Choice to switch to, the string "all" to reopen
over the full catalog, or None on cancel.
"""
from __future__ import annotations

from textual import on
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from rich.markup import escape

from rockycode.engine.providers import Choice
from rockycode.palette import LAVENDER, MUTED


class ModelPicker(ModalScreen):
    BINDINGS = [
        ("escape", "cancel", "cancel"),
        ("down", "cursor_down", "↓"),
        ("up", "cursor_up", "↑"),
    ]

    DEFAULT_CSS = """
    ModelPicker { align: center middle; }
    ModelPicker > #picker {
        width: 76%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $surface;
        border: round $primary;
        border-title-color: $text-muted;
    }
    ModelPicker OptionList { height: auto; max-height: 14; background: $surface; }
    ModelPicker #model-preview { height: auto; margin-top: 1; color: $text-muted; }
    ModelPicker #model-guide { height: 1; margin-top: 1; color: $text-muted; }
    """

    def __init__(self, choices: list[Choice], *, current: str, hidden: int = 0) -> None:
        super().__init__()
        self.choices = choices
        self.current = current  # the live engine's "eid:model"
        self.hidden = hidden    # keyless choices not shown (short view); 0 = full catalog

    def compose(self):
        with Vertical(id="picker") as box:
            box.border_title = "/model — pick provider + model (this session)"
            yield OptionList(id="model-list")
            yield Static("", id="model-preview")
            yield Static(
                f"[{MUTED}]↑↓ or click to move · ↵ switch · esc cancel · "
                f"direct next time: [{LAVENDER}]/model <provider>[:model][/][/]",
                id="model-guide",
            )

    def on_mount(self) -> None:
        ol = self.query_one("#model-list", OptionList)
        start = 0
        for i, c in enumerate(self.choices):
            cur = c.id == self.current
            if cur:
                start = i
            marker = "▸ " if cur else "  "
            eye = "  ·  ❖ sees images" if c.provider.vision else ""
            tag = "" if c.configured else "  ·  ✗ no key"
            ol.add_option(Option(f"{marker}{c.id:<30} — {escape(c.provider.label)}{eye}{tag}", id=str(i)))
        if self.hidden > 0:
            ol.add_option(Option(
                f"  … {self.hidden} more (no key yet) — show the full catalog", id="all"))
        ol.highlighted = start  # open ON the live model, not at the top
        ol.focus()
        self._show_preview(start)

    def _show_preview(self, idx: int | None) -> None:
        pv = self.query_one("#model-preview", Static)
        if idx is None or not (0 <= idx < len(self.choices)):
            pv.update(f"[{MUTED}]the full catalog — keys live in ~/.rockycode/.env as "
                      f"ROCKYCODE_<PROVIDER>[_CN]_API_KEY[/]")
            return
        c = self.choices[idx]
        key_line = (f"key {c.endpoint.key_env} ✓ set" if c.configured
                    else f"key {c.endpoint.key_env} ✗ not set — add it to ~/.rockycode/.env")
        vision = " · ❖ sees images" if c.provider.vision else ""
        pv.update(f"[{MUTED}]{escape(c.endpoint.base_url)} · reasoning {c.provider.reasoning}"
                  f" · tools {c.provider.tools}{vision}\n{escape(key_line)}[/]")

    @on(OptionList.OptionHighlighted)
    def _on_highlight(self, e: OptionList.OptionHighlighted) -> None:
        oid = e.option.id
        self._show_preview(int(oid) if oid and oid.isdigit() else None)

    @on(OptionList.OptionSelected)
    def _on_select(self, e: OptionList.OptionSelected) -> None:
        oid = e.option.id
        if oid == "all":
            self.dismiss("all")
        elif oid and oid.isdigit():
            self.dismiss(self.choices[int(oid)])

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_cursor_down(self) -> None:
        self.query_one("#model-list", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#model-list", OptionList).action_cursor_up()
