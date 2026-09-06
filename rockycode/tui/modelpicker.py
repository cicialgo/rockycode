"""The model picker: bare /model opens this — MODEL first, URL second.

Step 1 lists one row per model (kimi-k3, not kimi-cn:kimi-k3 AND
kimi-en:kimi-k3 — an EN/CN catalog stays short). Picking a model with a
single endpoint switches straight away; with several (regional cn/en, or an
own-URL entry from ~/.rockycode/endpoints.toml) the EndpointPicker follows:
pick which base URL serves it, or take the "custom base URL" row and type
your own — that URL is remembered as `<provider>-custom`. Heavy users
graduate to `/model <spec>`; the guide line teaches that at the moment of
use — same teach-at-use pattern as the mode picker.

ModelPicker dismiss() value: a list[Choice] (the picked model's endpoints —
the app applies a single choice directly and opens EndpointPicker for more),
the string "all" to reopen over the full catalog, or None on cancel.
EndpointPicker dismiss() value: a Choice, ("custom", <typed base_url>), or
None on cancel.
"""
from __future__ import annotations

from textual import on
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from rich.markup import escape

from rockycode.engine.providers import Choice
from rockycode.palette import LAVENDER, MUTED

_PICKER_CSS = """
    {name} {{ align: center middle; }}
    {name} > #picker {{
        width: 76%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $surface;
        border: round $primary;
        border-title-color: $text-muted;
    }}
    {name} OptionList {{ height: auto; max-height: 14; background: $surface; }}
    {name} #model-preview {{ height: auto; margin-top: 1; color: $text-muted; }}
    {name} #model-guide {{ height: 1; margin-top: 1; color: $text-muted; }}
    {name} #custom-url {{ display: none; margin-top: 1; }}
"""


def group_choices(choices: list[Choice]) -> list[list[Choice]]:
    """Flat (endpoint, model) choices → one group per (provider, model),
    original order kept. Each group is the same model across its endpoints."""
    groups: dict[tuple[str, str], list[Choice]] = {}
    order: list[tuple[str, str]] = []
    for c in choices:
        k = (c.provider.name, c.model)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(c)
    return [groups[k] for k in order]


class ModelPicker(ModalScreen):
    BINDINGS = [
        ("escape", "cancel", "cancel"),
        ("down", "cursor_down", "↓"),
        ("up", "cursor_up", "↑"),
    ]

    DEFAULT_CSS = _PICKER_CSS.format(name="ModelPicker")

    def __init__(self, choices: list[Choice], *, current: str, hidden: int = 0) -> None:
        super().__init__()
        self.groups = group_choices(choices)
        self.current = current  # the live engine's "eid:model"
        self.hidden = hidden    # keyless choices not shown (short view); 0 = full catalog

    def compose(self):
        with Vertical(id="picker") as box:
            box.border_title = "/model — pick a model (this session)"
            yield OptionList(id="model-list")
            yield Static("", id="model-preview")
            yield Static(
                f"[{MUTED}]↑↓ or click to move · ↵ pick · esc cancel · "
                f"direct next time: [{LAVENDER}]/model <provider>[:model][/][/]",
                id="model-guide",
            )

    def on_mount(self) -> None:
        ol = self.query_one("#model-list", OptionList)
        start = 0
        for i, g in enumerate(self.groups):
            cur = any(c.id == self.current for c in g)
            if cur:
                start = i
            marker = "▸ " if cur else "  "
            eye = "  ·  ❖ sees images" if g[0].vision else ""
            tag = "" if any(c.configured for c in g) else "  ·  ✗ no key"
            urls = f"  ·  {len(g)} URLs" if len(g) > 1 else ""
            ol.add_option(Option(
                f"{marker}{g[0].model:<30} — {escape(g[0].provider.label)}{eye}{tag}{urls}",
                id=str(i)))
        if self.hidden > 0:
            ol.add_option(Option(
                f"  … {self.hidden} more (no key yet) — show the full catalog", id="all"))
        ol.highlighted = start  # open ON the live model, not at the top
        ol.focus()
        self._show_preview(start)

    def _show_preview(self, idx: int | None) -> None:
        pv = self.query_one("#model-preview", Static)
        if idx is None or not (0 <= idx < len(self.groups)):
            pv.update(f"[{MUTED}]the full catalog — keys live in ~/.rockycode/.env as "
                      f"ROCKYCODE_<PROVIDER>[_CN]_API_KEY[/]")
            return
        g = self.groups[idx]
        vision = " · ❖ sees images" if g[0].vision else ""
        head = (f"[{MUTED}]reasoning {g[0].provider.reasoning} · tools "
                f"{g[0].provider.tools}{vision}")
        lines = [f"{escape(c.prov_id)}  {escape(c.endpoint.base_url)}  "
                 f"{'✓ key set' if c.configured else '✗ ' + c.endpoint.key_env}"
                 for c in g]
        pv.update(head + "\n" + "\n".join(lines) + "[/]")

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
            self.dismiss(self.groups[int(oid)])

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_cursor_down(self) -> None:
        self.query_one("#model-list", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#model-list", OptionList).action_cursor_up()


class EndpointPicker(ModalScreen):
    """Step 2: WHICH base URL serves the picked model — the provider's
    regional endpoints plus a "custom base URL" row (type your own; it is
    remembered in ~/.rockycode/endpoints.toml as `<provider>-custom`)."""

    BINDINGS = [
        ("escape", "cancel", "cancel"),
        ("down", "cursor_down", "↓"),
        ("up", "cursor_up", "↑"),
    ]

    DEFAULT_CSS = _PICKER_CSS.format(name="EndpointPicker")

    def __init__(self, group: list[Choice], *, current: str) -> None:
        super().__init__()
        self.group = group
        self.current = current

    def compose(self):
        with Vertical(id="picker") as box:
            box.border_title = f"/model — {self.group[0].model}: which URL?"
            yield OptionList(id="model-list")
            yield Static("", id="model-preview")
            yield Input(placeholder="https://your-gateway.example/v1  (↵ to save)",
                        id="custom-url")
            yield Static(
                f"[{MUTED}]↑↓ or click to move · ↵ switch · esc back · "
                f"direct next time: [{LAVENDER}]/model <endpoint>:<model>[/][/]",
                id="model-guide",
            )

    def on_mount(self) -> None:
        ol = self.query_one("#model-list", OptionList)
        start = 0
        for i, c in enumerate(self.group):
            cur = c.id == self.current
            if cur:
                start = i
            marker = "▸ " if cur else "  "
            key = "✓ key set" if c.configured else f"✗ {c.endpoint.key_env}"
            ol.add_option(Option(
                f"{marker}{c.prov_id:<16} {escape(c.endpoint.base_url):<40}  ·  {key}",
                id=str(i)))
        ol.add_option(Option("  ✎ custom base URL — your own gateway/proxy for "
                             f"{self.group[0].provider.name}", id="custom"))
        ol.highlighted = start
        ol.focus()

    @on(OptionList.OptionSelected)
    def _on_select(self, e: OptionList.OptionSelected) -> None:
        oid = e.option.id
        if oid == "custom":
            inp = self.query_one("#custom-url", Input)
            inp.styles.display = "block"
            self.query_one("#model-preview", Static).update(
                f"[{MUTED}]the URL is saved to ~/.rockycode/endpoints.toml and rides "
                f"the provider's key ({escape(self.group[0].endpoint.key_env)})[/]")
            inp.focus()
        elif oid and oid.isdigit():
            self.dismiss(self.group[int(oid)])

    @on(Input.Submitted, "#custom-url")
    def _on_url(self, e: Input.Submitted) -> None:
        url = e.value.strip()
        if url:
            self.dismiss(("custom", url))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_cursor_down(self) -> None:
        self.query_one("#model-list", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#model-list", OptionList).action_cursor_up()
