"""Image attachments: paste / attach, the route choice for text-only models, chips.

Part of the TUI shell (rockycode/tui/app.py), split out so each concern reads on its
own; the app class mixes this in and the methods keep `self` = the app.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from rich.markup import escape
from textual import on, work
from textual.containers import VerticalScroll
from textual.widgets import Static

from rockycode.i18n import tr, resolve_locale

from rockycode.palette import AMBER, LAVENDER, MUTED
from rockycode.tui import cardkeys
from rockycode.tui.cardkeys import CardKeys
from rockycode.tui.chatinput import ChatInput
from rockycode.tui.removal import gone


class _RouteRow(Static):
    def __init__(self, value: str, idx: int, card: "ImageRouteChoice") -> None:
        super().__init__("", id=f"route-opt-{idx}", classes="route-opt")
        self._value = value
        self._card = card

    def on_click(self) -> None:
        self._card._resolve(self._value)


class ImageRouteChoice(CardKeys, VerticalScroll):
    """Pick how rocky should understand attached images when the active model
    has no vision. Same inline pattern as InlineApproval (permission.py): docked
    above the input, transcript stays scrollable, ↑↓ + Enter or a click on a row,
    and the future resolves 'provider' | 'provider_always' | 'cli' | 'cli_always'
    | 'skip'. The *_always variant also persists `image = auto` to config — ask
    once, not every paste. Esc skips (sends the honest placeholder), never cancels
    the turn. It opens right behind the Enter that sent the message, so it follows
    the cards' keyboard rule (rockycode/tui/cardkeys.py).
    """

    can_focus = True

    BINDINGS = [
        ("up", "move(-1)", "up"),
        ("down", "move(1)", "down"),
        ("enter", "confirm", "select"),
        ("escape", "pick('skip')", "skip"),
    ]

    DEFAULT_CSS = """
    ImageRouteChoice {
        height: auto;
        max-height: 50%;
        overflow-y: auto;
        margin: 0 1 1 1;
        padding: 1 2;
        background: $surface;
        border: round $panel-lighten-2;
        border-title-color: $text-muted;
    }
    ImageRouteChoice:focus { border: round $primary; }
    ImageRouteChoice .route-opt { height: auto; min-height: 1; text-opacity: 55%; }
    ImageRouteChoice:focus .route-opt { text-opacity: 100%; }
    ImageRouteChoice #route-keys { color: $text-muted; margin-top: 1; }
    """

    KEYS_ID = "#route-keys"
    ESC_FROM_BOX = False  # its Esc sends the message without the image: from the message box, Esc stops the turn

    def __init__(self, n_images: int, provider_label: str, cli_label: str,
                 future: "asyncio.Future[str]", *, language: str = "en") -> None:
        super().__init__()
        self._language = resolve_locale(language)
        self._n = n_images
        self._future = future
        self._choices: list[tuple[str, str]] = []
        if provider_label:
            self._choices += [
                ("provider", tr('app.describe_via_this_time', self._language, p0=provider_label)),
                ("provider_always", tr('app.always_describe_via', self._language, p0=provider_label)),
            ]
        if cli_label:
            # Name the tool, not "it": `mmx describe {path}` → always row says
            # "always use mmx" so what gets saved to config is unmistakable.
            cli_name = cli_label.split()[0] if cli_label.split() else cli_label
            self._choices += [
                ("cli", tr('app.this_time', self._language, p0=cli_label)),
                ("cli_always", tr('app.always_use_for_images', self._language, p0=cli_name)),
            ]
        self._choices.append(("skip", tr('app.image_route_none', self._language)))
        self._idx = 0

    def compose(self):
        for i, (value, _text) in enumerate(self._choices):
            yield _RouteRow(value, i, self)
        yield Static(self._keys_markup(), id="route-keys")

    def _keys_markup(self) -> str:
        return tr('app.image_route_keys', self._language)

    def _invited(self) -> bool:
        return False  # it opens by itself, behind the message that carried the image

    def on_mount(self) -> None:
        plural = "s" if self._n > 1 and self._language == "en" else ""
        self.border_title = tr('app.image_this_model_can_t_see', self._language, p0=self._n, p1=plural)
        self._render_choices()

    def _render_choices(self) -> None:
        for i, (_value, text) in enumerate(self._choices):
            row = self.query_one(f"#route-opt-{i}", Static)
            if i == self._lit():
                row.update(f"[b {LAVENDER}]▸ {escape(text)}[/]")
                row.scroll_visible(animate=False)
            else:
                row.update(f"[{MUTED}]  {escape(text)}[/]")

    def action_move(self, delta: int) -> None:
        if self._move(delta, len(self._choices)):
            self._render_choices()

    def _confirm(self) -> None:
        self._resolve(self._choices[self._idx][0])

    def action_pick(self, choice: str) -> None:
        self._resolve(choice)

    def _resolve(self, value: str) -> None:
        if not self._future.done():
            self._future.set_result(value)


class ImagesMixin:
    @on(ChatInput.PasteRequested)
    def _on_paste_requested(self, _event: ChatInput.PasteRequested) -> None:
        self._paste_worker(text_fallback=True)

    @work(group="paste", exclusive=True, exit_on_error=False)
    async def _paste_worker(self, *, text_fallback: bool) -> None:
        """Read the OS clipboard (a worker — osascript/xclip block). An image
        becomes a pending [image #N] chip; with text_fallback (the Ctrl+V key,
        not /paste) clipboard TEXT is inserted instead, so the key always
        pastes something."""
        from rockycode.engine import images as images_mod
        from rockycode.tui import clipboard

        dest = images_mod.assets_dir(self.engine.trajectory.session_id)
        path = await asyncio.to_thread(clipboard.grab_image, dest)
        if path is not None:
            # cap huge pastes (Retina screenshots) before they hit the wire —
            # vision providers bill image tokens by dimensions
            path = await asyncio.to_thread(images_mod.downscale_for_api, path)
            await self._attach_image(str(path))
            return
        if text_fallback:
            text = await asyncio.to_thread(clipboard.grab_text)
            if text:
                self.query_one(ChatInput).insert(text)
                return
        self.notify(self._t('commands.clipboard_empty'), timeout=3)

    async def _attach_image(self, path: str) -> None:
        """Register a pending image + drop its chip into the input box."""
        self._img_seq += 1
        n = self._img_seq
        self._pending_images[n] = path
        inp = self.query_one(ChatInput)
        inp.insert(f"[image #{n}] ")
        inp.focus()
        kb = max(1, Path(path).stat().st_size // 1024)
        self.notify(self._t('commands.image_attached', p0=n, p1=Path(path).name, p2=kb), timeout=3)
        if not self.engine.vision_enabled:
            # Say what will ACTUALLY happen under the current setting, and name
            # the config vocabulary right where it matters — the picker says
            # "once/always" but the switch is /config image auto|ask|off|….
            from rockycode.config import load as load_config
            from rockycode.engine import vision as vision_mod
            setting = vision_mod.image_setting(load_config(self.engine.workdir)["image"])
            eyes = vision_mod.sidecar_choice(setting.preferred, strict=bool(setting.preferred))
            if setting.route == "cli":
                how = self._t('commands.image_cli')
            elif setting.route == "off":
                how = self._t('commands.image_off')
            elif setting.route == "ask":
                how = self._t('commands.image_ask')
            elif eyes is not None:  # auto, or a pinned endpoint that is keyed
                how = self._t('commands.image_auto', p0=eyes.model)
            else:
                how = self._t('commands.image_no_route')
            await self._add(Static(
                self._t('commands.image_model_blind', p1=escape(self.engine.model), p2=escape(how)),
                classes="tool-line"))

    async def _route_images(self, text: str, images: list[str]) -> list:
        """The no-vision path: decide a route (config `image`, or the
        inline picker on `ask`), then describe each image ONCE via
        engine/vision.py and store the description on its image_path part —
        history/trajectory carry it, images._flatten emits it. Any failure
        degrades to the honest placeholder; the turn always proceeds."""
        from rockycode.config import change_feedback, load as load_config, set_value
        from rockycode.engine import images as images_mod
        from rockycode.engine import vision

        setting = vision.image_setting(load_config(self.engine.workdir)["image"])
        route, preferred, template = setting.route, setting.preferred, setting.template
        # A pinned endpoint is honoured or refused, never swapped for another.
        choice = vision.sidecar_choice(preferred, strict=bool(preferred))
        if route == "off":
            return images
        if route == "provider" and choice is None:
            await self._add(Static(
                self._t('commands.image_no_provider', p1=escape(preferred)),
                classes="tool-line"))
            return images
        if route == "auto":
            # the smooth path: no picker — the registry's sidecar (deepseek-flash
            # on the home key, else the first keyed vision endpoint) describes
            # silently; otherwise one line saying how to get eyes, and the
            # turn proceeds.
            if choice is not None:
                route = "provider"
            else:
                await self._add(Static(
                    self._t('commands.image_no_auto', p1=escape(self.engine.model)),
                    classes="tool-line"))
                return images
        if route == "ask":
            if choice is None:
                await self._add(Static(
                    self._t('commands.image_setup', p1=escape(self.engine.model)),
                    classes="tool-line"))
                return images
            picked = await self._ask_image_route(len(images), choice.id, "")
            if picked.endswith("_always"):
                picked = picked.removesuffix("_always")
                _v, err = set_value("image", "auto")
                if not err:
                    await self._add(Static(
                        self._t('commands.image_route_saved', p1="auto"),
                        classes="tool-line"))
                    feedback = change_feedback("image", self.engine.workdir, self._language)
                    if feedback:
                        await self._add(Static(escape(feedback), classes="tool-line"))
            if picked == "skip":
                return images
            route = picked
        out: list = []
        question = text[:500]  # the user's message focuses the description
        for pth in images:
            part = pth if isinstance(pth, dict) else images_mod.image_part(pth)
            name = Path(part["path"]).name
            await self._add(Static(self._t('commands.image_looking', p1=escape(name)),
                                   classes="tool-line"))
            try:
                desc, src = await vision.describe(
                    part["path"], question, route=route,
                    preferred=preferred, template=template)
                part["description"], part["described_by"] = desc, src
                await self._add(Static(
                    self._t('commands.image_seen', p1=escape(name), p2=escape(src)),
                    classes="tool-line"))
            except vision.VisionError as e:
                await self._add(Static(
                    self._t('commands.image_error', p0=AMBER, p1=escape(name), p2=escape(str(e))),
                    classes="tool-line"))
            out.append(part)
        return out

    async def _ask_image_route(self, n: int, provider_label: str, cli_label: str) -> str:
        """Dock the route picker above the input and await the choice — same
        inline mechanics as _ask_inline, so the transcript stays scrollable."""
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        widget = ImageRouteChoice(n, provider_label, cli_label, fut, language=self._language)
        await self.mount(widget, before=self.query_one("#prompt"))
        try:
            return await fut
        finally:
            try:
                cardkeys.card_closing(self, widget)
            except Exception:  # noqa: BLE001 — app may be tearing down
                pass
            if widget.is_mounted:
                await gone(widget)

    def _collect_images(self, text: str) -> list[str]:
        """Images riding this message: every [image #N] chip still present in
        the text (deleting a chip detaches that image), plus any image-file
        path typed or dragged into the message — on a no-vision model those go
        through the describe route (_route_images), so drag-and-drop works
        everywhere. Pending chips clear on submit either way."""
        import re
        from rockycode.engine import images as images_mod

        found: list[str] = []
        for m in re.finditer(r"\[image #(\d+)\]", text):
            p = self._pending_images.get(int(m.group(1)))
            if p and p not in found:
                found.append(p)
        self._pending_images.clear()
        for p in images_mod.extract_image_paths(text):
            if str(p) not in found:
                found.append(str(p))
        return found

    # ---- input → engine ------------------------------------------------------
