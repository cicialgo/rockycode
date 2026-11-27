"""The model picker: bare /model opens this — MODEL first, URL second.

Step 1 lists one row per model (qwen3.8-max, not qwen:… AND qwen-plan:… —
the catalog stays short). Picking a model with a single endpoint switches
straight away; with several (the official URL, a subscription-plan URL, or
an own-URL entry from ~/.rockycode/endpoints.toml) the EndpointPicker
follows: pick which base URL serves it, or take the "custom base URL" row
and type your own — that URL is remembered as `<provider>-custom` and the
field opens pre-filled on every later visit (the saved row is not a dead
end). Heavy
users graduate to `/model <spec>`; the guide line teaches that at the moment
of use — same teach-at-use pattern as the mode picker.

ModelPicker dismiss() value: a list[Choice] (the picked model's endpoints —
the app applies a single choice directly and opens EndpointPicker for more),
the string "all" to reopen over the full catalog, the string "keys" for the
"add or change a key" row (tui/keyprompt.py), or None on cancel.
EndpointPicker dismiss() value: a Choice, ("custom", <typed base_url>), or
None on cancel.
"""
from __future__ import annotations

from textual import on
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from rich.markup import escape

from rockycode.engine.providers import Choice, fmt_tokens
from rockycode.i18n import tr
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
    {name} #model-guide {{ height: auto; margin-top: 1; color: $text-muted; }}
    {name} #custom-url {{ display: none; margin-top: 1; }}
    {name} #custom-url.on {{ display: block; }}
"""


def _key_note(c: Choice, language: str = "en") -> str:
    """The key column for an endpoint row — a keyless LOCAL endpoint says so
    instead of pretending a key was set."""
    if c.endpoint.keyless:
        return tr("selectors.key.local", language)
    if c.configured:
        src = c.endpoint.key_source()
        # an older `_CN_`/`_EN_` name still works — say so, and name the new one
        return (tr("selectors.key.set", language) if src == c.endpoint.key_env else
                tr("selectors.key.rename", language, source=escape(src), target=escape(c.endpoint.key_env)))
    return f"✗ {escape(c.endpoint.key_env)}"


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

    def __init__(self, choices: list[Choice], *, current: str, hidden: int = 0,
                 hints: list[str] | None = None, language: str = "en") -> None:
        super().__init__()
        self.language = language
        self.groups = group_choices(choices)
        self.current = current  # the live engine's "eid:model"
        self.hidden = hidden    # unkeyed choices not shown (short view); 0 = full catalog
        # Local providers with nothing to offer right now ("ollama — not
        # running · start it: ollama serve") — dimmed, unpickable rows, so a
        # local option is discoverable even when its server is down.
        self.hints = hints or []

    def compose(self):
        with VerticalScroll(id="picker") as box:
            box.border_title = tr("selectors.model.title", self.language)
            yield OptionList(id="model-list")
            yield Static("", id="model-preview")
            yield Static(
                tr("selectors.model.guide", self.language, muted=MUTED, accent=LAVENDER),
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
            eye = tr("selectors.model.vision", self.language) if g[0].vision else ""
            tag = "" if any(c.configured for c in g) else tr("selectors.model.no_key", self.language)
            urls = tr("selectors.model.urls", self.language, count=len(g)) if len(g) > 1 else ""
            ol.add_option(Option(
                f"{marker}{escape(g[0].model):<30} — {escape(g[0].provider.label)}{eye}{tag}{urls}",
                id=str(i)))
        for j, hint in enumerate(self.hints):
            # disabled = unpickable and auto-dimmed; the row is a signpost
            ol.add_option(Option(f"  ◌ {escape(hint)}", id=f"hint-{j}", disabled=True))
        if self.hidden > 0:
            ol.add_option(Option(
                tr("selectors.model.more", self.language, count=self.hidden), id="all"))
        # A key that exists but stopped working has no other door: the picker
        # only asks for a key when one is missing.
        ol.add_option(Option(tr("selectors.model.keys", self.language), id="keys"))
        ol.highlighted = start  # open ON the live model, not at the top
        ol.focus()
        self._show_preview(start)

    def _show_preview(self, idx: int | None, row: str = "") -> None:
        pv = self.query_one("#model-preview", Static)
        if row == "keys":
            pv.update(tr("selectors.model.keys_hint", self.language, muted=MUTED))
            return
        if idx is None or not (0 <= idx < len(self.groups)):
            pv.update(tr("selectors.model.catalog", self.language, muted=MUTED))
            return
        g = self.groups[idx]
        spec = g[0].spec
        vision = tr("selectors.model.vision", self.language) if g[0].vision else ""
        head = tr("selectors.model.details", self.language, muted=MUTED, context=fmt_tokens(spec.context),
                  output=fmt_tokens(spec.max_output), reasoning=g[0].provider.reasoning,
                  tools=g[0].provider.tools, vision=vision)
        if spec.label:
            head += f"\n{escape(spec.label)}"
        lines = [f"{escape(c.prov_id)}  {escape(c.endpoint.base_url)}  {_key_note(c, self.language)}"
                 for c in g]
        pv.update(head + "\n" + "\n".join(lines) + "[/]")

    @on(OptionList.OptionHighlighted)
    def _on_highlight(self, e: OptionList.OptionHighlighted) -> None:
        oid = e.option.id
        self._show_preview(int(oid) if oid and oid.isdigit() else None, oid or "")

    @on(OptionList.OptionSelected)
    def _on_select(self, e: OptionList.OptionSelected) -> None:
        oid = e.option.id
        if oid in ("all", "keys"):
            self.dismiss(oid)
        elif oid and oid.isdigit():
            self.dismiss(self.groups[int(oid)])

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_cursor_down(self) -> None:
        self.query_one("#model-list", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#model-list", OptionList).action_cursor_up()


class EndpointPicker(ModalScreen):
    """Step 2: WHICH base URL serves the picked model — the official
    endpoint, a subscription-plan endpoint when the provider has one, an
    own-URL row already saved, plus a "custom base URL" row (type your own;
    it is remembered in ~/.rockycode/endpoints.toml as `<provider>-custom`).

    The saved own-URL row stays editable on every later open. Selecting it
    (or the "custom base URL" row) opens the field pre-filled with the URL
    already on file — Enter on that row used to re-apply it and close, so
    the field only worked the first time."""

    BINDINGS = [
        ("escape", "cancel", "cancel"),
        ("down", "cursor_down", "↓"),
        ("up", "cursor_up", "↑"),
    ]

    DEFAULT_CSS = _PICKER_CSS.format(name="EndpointPicker")

    def __init__(self, group: list[Choice], *, current: str, language: str = "en") -> None:
        super().__init__()
        self.language = language
        self.group = group
        self.current = current

    def compose(self):
        with VerticalScroll(id="picker") as box:
            box.border_title = tr("selectors.endpoint.title", self.language, model=escape(self.group[0].model))
            yield OptionList(id="model-list")
            yield Static("", id="model-preview")
            yield Input(placeholder=tr("selectors.endpoint.placeholder", self.language),
                        id="custom-url", select_on_focus=False)
            yield Static(
                tr("selectors.endpoint.guide", self.language, muted=MUTED, accent=LAVENDER),
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
            what = f" ({escape(c.endpoint.label)})" if c.endpoint.label else ""
            ol.add_option(Option(
                f"{marker}{escape(c.prov_id):<14}{what:<22} {escape(c.endpoint.base_url):<44}  ·  {_key_note(c, self.language)}",
                id=str(i)))
        ol.add_option(Option(tr("selectors.endpoint.custom", self.language, provider=escape(self.group[0].provider.name)), id="custom"))
        ol.highlighted = start
        # Coming back to a URL you already saved: the field is on screen,
        # pre-filled, before any keypress. Focus stays on the list so ↑↓/↵
        # still pick a different endpoint; ↵ on the saved row (or the ✎ row)
        # moves into the field.
        saved = self._saved_custom()
        if saved is not None and saved.id == self.current:
            self._reveal_url_editor(focus=False)
        ol.focus()

    def _saved_custom(self) -> Choice | None:
        """The `<provider>-custom` endpoint already on file, if this model has one."""
        eid = f"{self.group[0].provider.name}-custom"
        for c in self.group:
            if c.endpoint.eid == eid:
                return c
        return None

    def _reveal_url_editor(self, *, focus: bool) -> None:
        """Show the URL field. A saved URL is pre-filled when the field is
        still empty, so the second open edits the last value instead of
        starting from a blank box that forgets it."""
        inp = self.query_one("#custom-url", Input)
        saved = self._saved_custom()
        if saved is not None and not inp.value.strip():
            inp.value = saved.endpoint.base_url
        if not inp.has_class("on"):
            inp.add_class("on")
        self.query_one("#model-preview", Static).update(
            tr("selectors.endpoint.edit", self.language,
               key_env=escape(self.group[0].endpoint.key_env)))
        if focus:
            inp.focus()
        # After focus too: select_on_focus is off, but a pre-filled URL should
        # still land the cursor at the end so the next key edits it.
        if inp.value:
            inp.cursor_position = len(inp.value)

    @on(OptionList.OptionSelected)
    def _on_select(self, e: OptionList.OptionSelected) -> None:
        oid = e.option.id
        if oid == "custom":
            self._reveal_url_editor(focus=True)
            return
        if not (oid and oid.isdigit()):
            return
        choice = self.group[int(oid)]
        # The cached `<provider>-custom` row is the URL itself, not a finished
        # choice. Re-applying it here is what made the field uneditable the
        # second time the picker opened on it.
        if choice.endpoint.eid == f"{choice.provider.name}-custom":
            self._reveal_url_editor(focus=True)
            return
        self.dismiss(choice)

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
