"""Collaboration modes (/research, /learn): the picker, the chip, the prompt swap.

Part of the TUI shell (rockycode/tui/app.py), split out so each concern reads on its
own; the app class mixes this in and the methods keep `self` = the app.
"""
from __future__ import annotations

from rich.markup import escape
from textual import work
from textual.widgets import Static


from rockycode.palette import AMBER, LAVENDER, MUTED
from rockycode.tui.chatinput import ChatInput


class ModeMixin:
    def _render_mode_chip(self) -> None:
        name = self.engine.mode_name
        self.query_one("#modechip", Static).update(
            f"[{LAVENDER}]◉ {name}[/]" if name else "")

    async def _handle_mode_cmd(self, family: str, text: str) -> None:
        """/research and /learn — bare opens the picker (or applies the single
        typeless mode), `<name>` is the direct path, `off` leaves, `always`
        makes it this folder's launch default, `never` clears that default."""
        from rockycode.engine.modes import discover, resolve

        args = text.split()[1:]
        arg = args[0].lower() if args else ""
        if arg == "off":
            if self.engine.mode_name is None:
                await self._add(Static(self._t('app.no_mode_active'), classes="tool-line"))
            else:
                self.engine.clear_mode()
                self._render_mode_chip()
                await self._add(Static(
                    self._t('app.mode_off_plain_rocky_again'), classes="tool-line"))
            return
        if arg == "always":
            name = args[1].lower() if len(args) > 1 else (self.engine.mode_name or "")
            if not name:
                await self._add(Static(
                    self._t('app.mode_nothing_to_persist', p1=family, p2=family), classes="tool-line"))
                return
            # Built-ins only: a folder default is applied silently at launch, so a
            # cloned repo's local mode file must never ride in through it.
            mode, err = resolve(family, name, workdir=self.engine.workdir, builtin_only=True)
            if mode is None:
                await self._add(Static(
                    self._t('app.mode_builtin_only', p1=escape(err)),
                    classes="tool-line"))
                return
            from rockycode.config import set_project_value
            _v, werr = set_project_value(self.engine.workdir, "mode", mode.name)
            if werr:
                await self._add(Static(f"[{AMBER}]? {escape(werr)}[/]", classes="tool-line"))
                return
            if self.engine.mode_name != mode.name:
                await self._apply_mode(family, mode)
            await self._add(Static(
                self._t('app.mode_default_saved', p1=mode.name, p2=family), classes="tool-line"))
            return
        if arg == "never":
            from rockycode.config import set_project_value
            _v, werr = set_project_value(self.engine.workdir, "mode", "")
            await self._add(Static(
                f"[{AMBER}]? {escape(werr)}[/]" if werr else
                self._t('app.mode_default_cleared'),
                classes="tool-line"))
            return
        if arg:
            mode, err = resolve(family, arg, workdir=self.engine.workdir)
            if mode is None:
                await self._add(Static(f"[{MUTED}]· {escape(err)}[/]", classes="tool-line"))
                return
            await self._apply_mode(family, mode)
            return
        # Bare command: a single-mode family (typeless /learn) applies directly;
        # otherwise — or when a mode is active, so "off" stays reachable — picker.
        modes = list(discover(self.engine.workdir).get(family, {}).values())
        if not modes:
            await self._add(Static(self._t('app.no_modes_installed', p1=family), classes="tool-line"))
        elif len(modes) == 1 and self.engine.mode_name is None:
            await self._apply_mode(family, modes[0])
        else:
            self._mode_picker_flow(family, modes)

    @work
    async def _mode_picker_flow(self, family: str, modes: list) -> None:
        from rockycode.tui.modepicker import ModePicker

        result = await self.push_screen_wait(
            ModePicker(family, modes, active=self.engine.mode_name, language=self._language))
        if result == "off":
            self.engine.clear_mode()
            self._render_mode_chip()
            await self._add(Static(
                self._t('app.mode_off_plain_rocky_again'), classes="tool-line"))
        elif result is not None:
            await self._apply_mode(family, result)
        self.query_one(ChatInput).focus()

    async def _apply_mode(self, family: str, mode) -> None:
        """Swap the contract in + the entry card that teaches the direct path
        (same teach-at-use pattern as the resume exit card)."""
        self.engine.set_mode(mode.name, mode.body)
        self._render_mode_chip()
        key = f"selectors.mode.{mode.name}.description"
        description = self._t(key) if mode.builtin else key
        if description == key:
            description = mode.description
        await self._add(Static(
            f"[bold {LAVENDER}]◉ {escape(mode.name)}[/] [{MUTED}]— {escape(description)}[/]",
            classes="tool-line"))
        direct = f"/{family}" if mode.name == family else f"/{family} {mode.name}"
        await self._add(Static(
            self._t('app.mode_leave_hint', p1=family, p2=family, p3=direct), classes="tool-line"))
