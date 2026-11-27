"""Add or change a provider key without leaving the app.

`/model` on a model whose provider has no key used to print three lines about
an env file and stop: quit, edit ~/.rockycode/.env by hand, start again. Now
the key is taken right there — typed hidden, tested with one free call, saved
by the same saver the first run uses (onboarding.save_key), applied to the
running process. Where the machine has an OS keychain the key goes there
unless the user picks the key file (0600, every other line kept); a keychain
that does not take the key saves nothing, and the field stays open. To change a
key that exists, the `/model` picker has an "add or change a key" row:
KeyPicker asks which endpoint and says where each key lives, then the same
field opens. Its last row moves the keys still in the key file to the keychain.

There is no command that takes a key, or anything typed next to one: a key
lives only in KeyScreen's field, never in the chat input or its history,
never in the transcript, never in a request to a model.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from rich.cells import cell_len
from rich.markup import escape
from textual import on
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from rockycode import onboarding
from rockycode.i18n import tr
from rockycode.palette import AMBER


def _place(ep, language: str) -> str:
    """Where an endpoint's key lives — for a row's label, never the value."""
    name = ep.key_source() or ep.key_env
    place = onboarding.key_place(name) if ep.key() else ""
    if not place and ep.key_env in onboarding.keychain_silent():
        place = "silent"
    return tr(f"key_prompt.place_{place or 'none'}", language)


class KeyPicker(ModalScreen):
    """Which endpoint is the key for? One row per endpoint that takes a key,
    saying where its key lives. Dismisses with (provider, endpoint), "move"
    (the keys in the key file → the OS keychain), or None."""

    BINDINGS = [("escape", "cancel", "cancel")]

    DEFAULT_CSS = """
    KeyPicker { align: center middle; }
    KeyPicker > #picker {
        width: 76%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $surface;
        border: round $primary;
        border-title-color: $text-muted;
    }
    KeyPicker OptionList { height: auto; max-height: 16; background: $surface; }
    KeyPicker #keys-guide { height: auto; margin-top: 1; color: $text-muted; }
    """

    def __init__(self, rows: list, *, current: str = "", language: str = "en") -> None:
        super().__init__()
        self.rows, self.current, self.language = rows, current, language  # current: the endpoint in use

    def compose(self):
        with VerticalScroll(id="picker") as box:
            box.border_title = tr("key_prompt.pick_title", self.language)
            yield OptionList(id="keys-list")
            yield Static(tr("key_prompt.pick_guide", self.language), id="keys-guide")

    def on_mount(self) -> None:
        rows = self.query_one("#keys-list", OptionList)
        start = 0
        labels = [prov.label + (f" · {ep.label}" if ep.label else "") for prov, ep in self.rows]
        width = max(map(cell_len, labels), default=0)  # one column for "where", CJK labels included
        for i, ((_, ep), label) in enumerate(zip(self.rows, labels)):
            if ep.eid == self.current:
                start = i
            rows.add_option(Option(f"{'▸ ' if ep.eid == self.current else '  '}{escape(label)}"
                                   f"{' ' * (width - cell_len(label) + 2)}{_place(ep, self.language)}", id=str(i)))
        plain = len(onboarding.keys_in_file()) if onboarding.keychain_available() else 0
        if plain:  # the keys already here get the same safer home, without pasting each again
            rows.add_option(Option(tr("key_prompt.move", self.language, count=plain), id="move"))
        rows.highlighted = start if self.rows else None  # open ON the endpoint in use
        rows.focus()

    @on(OptionList.OptionSelected, "#keys-list")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss("move" if event.option.id == "move" else self.rows[int(event.option.id)])

    def action_cancel(self) -> None:
        self.dismiss(None)


class KeyScreen(ModalScreen):
    """One key for one endpoint. Dismisses with (where it was saved, verdict)
    — verdict "ok" or "unverified" (the endpoint could not be reached) — or
    None when the user cancels. A keychain that does not take the key saves
    nothing: the field stays open with the key in it, and the user unlocks and
    tries again or picks the key file row."""

    BINDINGS = [("escape", "cancel", "cancel")]
    AUTO_FOCUS = "#key-input"  # the field is ready to paste into

    DEFAULT_CSS = """
    KeyScreen { align: center middle; }
    KeyScreen > #picker {
        width: 76%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $surface;
        border: round $primary;
        border-title-color: $text-muted;
    }
    KeyScreen #key-body { height: auto; color: $text-muted; }
    KeyScreen #key-input { margin-top: 1; }
    KeyScreen #key-status { height: auto; margin-top: 1; }
    KeyScreen OptionList { height: auto; margin-top: 1; background: $surface; }
    KeyScreen #key-guide { height: auto; margin-top: 1; color: $text-muted; }
    """

    def __init__(self, label: str, key_env: str, base_url: str, *, language: str = "en") -> None:
        super().__init__()
        self.label, self.key_env, self.base_url, self.language = label, key_env, base_url, language
        self.host = urlsplit(base_url).hostname or base_url
        self.keychain = onboarding.keychain_available()  # then it is the first, default row
        self._busy = False

    def compose(self):
        with VerticalScroll(id="picker") as box:
            box.border_title = tr("key_prompt.title", self.language, label=escape(self.label))
            where = escape(onboarding._global_env_display())
            yield Static(tr("key_prompt.body", self.language, label=escape(self.label)).strip(), id="key-body")
            yield Input(password=True, placeholder=tr("key_prompt.placeholder", self.language), id="key-input")
            # A key the keychain holds but did not give is not a key never added: say so up front.
            silent = self.key_env in onboarding.keychain_silent()
            yield Static(f"[{AMBER}]{tr('key_prompt.silent', self.language)}[/]" if silent else "", id="key-status")
            saves = ([Option(tr("key_prompt.save_keychain", self.language), id="keychain")] if self.keychain else [])
            yield OptionList(*saves, Option(tr("key_prompt.save_file", self.language, where=where), id="file"),
                             Option(tr("key_prompt.cancel", self.language), id="cancel"), id="key-actions")
            yield Static(tr("key_prompt.guide_keychain" if self.keychain else "key_prompt.guide", self.language),
                         id="key-guide")

    def _say(self, text: str = "") -> None:
        self.query_one("#key-status", Static).update(text)

    @on(Input.Submitted, "#key-input")
    async def _submitted(self, _event: Input.Submitted) -> None:
        await self._save(keychain=self.keychain)  # ↵ = the first row

    @on(OptionList.OptionSelected, "#key-actions")
    async def _picked(self, event: OptionList.OptionSelected) -> None:
        if event.option.id == "cancel":
            self.action_cancel()
        else:
            await self._save(keychain=event.option.id == "keychain")

    async def _save(self, *, keychain: bool) -> None:
        if self._busy:
            return
        field = self.query_one("#key-input", Input)
        key = field.value.strip()
        if not key:
            self._say(f"[{AMBER}]{tr('key_prompt.empty', self.language)}[/]")
            field.focus()
            return
        self._busy = True
        field.disabled = True
        self._say(tr("key_prompt.testing", self.language, host=escape(self.host)))
        try:
            # One free, authenticated call (GET /models); off the UI thread.
            verdict = await asyncio.to_thread(onboarding.probe_key, key, self.base_url)
            if verdict == "rejected":
                field.value = ""
                self._say(f"[{AMBER}]{tr('key_prompt.rejected', self.language)}[/]")
                return
            # The keychain can ask (an OS dialog) and take its time: off the UI thread too.
            where = await asyncio.to_thread(onboarding.save_key, self.key_env, key, prefer_keyring=keychain)
        except onboarding.KeychainRefused:  # nothing saved; the plain-text file is theirs to pick
            self._say(f"[{AMBER}]{tr('key_prompt.keychain_refused', self.language)}[/]")
            return
        except OSError as error:  # the key file could not be written
            self._say(f"[{AMBER}]{escape(tr('key_prompt.unsaved', self.language, reason=str(error)))}[/]")
            return
        finally:
            self._busy = False
            field.disabled = False
            if self.is_attached:
                field.focus()
        field.value = ""  # the key leaves the screen with nothing but where it went
        self.dismiss((where, verdict))

    def action_cancel(self) -> None:
        if not self._busy:
            self.dismiss(None)
