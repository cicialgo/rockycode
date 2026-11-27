"""The host side of engine/approval.py: the inline approval card, the
permission chip / picker / shift+tab cycle, the waiting list of steps yolo
parked, and the names the rest of the app uses for the pipeline's state.

Part of the TUI shell (rockycode/tui/app.py), split out so each concern reads on its
own; the app class mixes this in and the methods keep `self` = the app.
"""
from __future__ import annotations

import asyncio
import json

from rich.markup import escape
from textual import work
from textual.css.query import NoMatches
from textual.widgets import Static

from rockycode.engine.approval import tool_detail

from rockycode.palette import AMBER, MUTED, RED, VIOLET
from rockycode.tui import cardkeys
from rockycode.tui.chatinput import ChatInput
from rockycode.tui.widgets import ParkChip, PermChip
from rockycode.tui.removal import gone


class ParkedLine(Static):
    """The line a turn leaves when it parked something. Clicking it opens the cards."""

    def on_click(self) -> None:
        self.app.open_parked()


class ApprovalMixin:
    @property
    def _permission_mode(self) -> str:
        return self._approver.mode

    @_permission_mode.setter
    def _permission_mode(self, mode: str) -> None:
        self._approver.mode = mode

    @property
    def _auto_approve(self) -> set[str]:
        return self._approver.session.tools      # non-bash tools OK'd this session

    @property
    def _auto_approve_bins(self) -> set[str]:
        return self._approver.session.bins       # bash BINARIES OK'd this session

    @property
    def _jev(self):
        return self._approver.verifier

    @_jev.setter
    def _jev(self, gate) -> None:
        self._approver.verifier = gate

    @property
    def _tick_loop(self):
        return self._approver.tick.loop          # the Loop whose tick runs right now

    @_tick_loop.setter
    def _tick_loop(self, loop) -> None:
        self._approver.tick.loop = loop

    @property
    def _tick_blocked(self):
        return self._approver.tick.blocked       # first ask-tier denial in that tick

    @_tick_blocked.setter
    def _tick_blocked(self, value) -> None:
        self._approver.tick.blocked = value

    @property
    def _tick_once(self) -> set:
        return self._approver.tick.once          # (tool, detail) one-shots from /loop allow

    async def _approve_tool(self, name: str, args: dict) -> bool:
        """The engine's approver is the shared pipeline; this name stays for
        callers that hold the bound method."""
        return await self._approver(name, args)

    def _grant_read_if_escaping(self, name: str, args: dict) -> None:
        self._approver.grant_read_if_escaping(name, args)  # a /loop allow one-shot is a human grant

    async def _ask_request(self, req) -> str:
        """The pipeline needs a human: mount the inline card. A dangerous bash
        command gets no 'allow for this session' option (session_grant=None)."""
        label = None
        if req.session_grant:
            label = (self._t('app.allow_for_this_session', p0=req.session_grant) if req.tool == "bash"
                     else self._t('app.allow_tool_for_session', p0=req.session_grant))
        return await self._ask_inline(req.tool, req.detail, req.risk, req.warning, session_label=label)

    async def _on_approval_note(self, kind: str, **info) -> None:
        """What the pipeline decided without a prompt, said in the transcript."""
        detail = str(info.get("detail", ""))
        if kind == "blocked":
            # A never-allowed command. Refused in every mode — this is the
            # system blocking, not the user denying, so say so loudly.
            await self._add(Static(
                self._t('app.blocked_a_dangerous_command', p1=escape(detail)), classes="error-msg"))
        elif kind == "auto_allowed":
            await self._add(Static(
                self._t('app.jev_auto_allowed', p1=escape(detail[:180])), classes="tool-line"))
        elif kind == "auto_denied":
            err = info.get("error")
            label = (self._t('app.jev_auto_mode_failed_closed', p0=err) if err
                     else self._t('app.jev_auto_mode_denied'))
            await self._add(Static(
                f"[{AMBER}]· {escape(label)}[/] [dim]{escape(detail[:180])}[/]", classes="tool-line"))
        elif kind == "parked":
            # yolo did not stop to ask. The tool line says "parked"; the chip keeps count.
            self._render_park_chip()

    # ---- parked steps: yolo never waits ------------------------------------
    # A step only the user can allow is refused in yolo and kept on the approver's
    # waiting list. The chip counts it, the turn that parked it leaves one clickable
    # line, and the card opens when the user asks for it — never on its own, so a key
    # typed for the next message cannot land on it.

    def _render_park_chip(self) -> None:
        try:
            chip = self.query_one("#parkchip", ParkChip)
        except NoMatches:
            return  # a screen is on top of chat — the chip isn't visible now
        waiting = len(self._approver.parked)
        chip.display = bool(waiting)
        chip.update(self._t('parked.chip', n=waiting))
        chip.tooltip = self._t('parked.chip_tooltip')

    async def _after_parking(self) -> None:
        """The end of a turn that parked something: say so once, in a line that opens the cards."""
        approver = self._approver
        if approver.stop_turn:
            await self._add(Static(self._t('parked.stopped', n=approver.turn_parks), classes="tool-line"))
        if approver.turn_parks and approver.parked:
            stick = self._at_bottom()
            await self._add(ParkedLine(self._t('parked.reminder', n=len(approver.parked)), classes="tool-line"))
            if stick:
                # The line is the turn's last one and a place to click. The scroll _add asks for
                # can run before the line is laid out and stop one row short; ask again after that.
                self.call_after_refresh(self._show_last_line)
        self._render_park_chip()
        self._tell_not_run(turn_done=True)

    def _show_last_line(self) -> None:
        try:
            self._transcript().scroll_end(animate=False)
        except NoMatches:  # a screen is on top of the chat, or the app is closing
            pass

    def _back_to_the_end(self) -> None:
        """Once the layout has settled: a transcript that is within two rows of its end goes
        to its end. A user who scrolled up to read is left where they are."""
        if self._at_bottom():
            self._show_last_line()

    def _tell_not_run(self, turn_done: bool = False) -> None:
        """rocky should not go on thinking a step waits that the user chose not to
        run. Said between turns only (`turn_done`: the engine's turn just ended):
        a note in the middle of one would split a tool call from its result."""
        worker = self._turn_worker
        busy = self._delivery.running or (worker is not None and worker.is_running)
        if self._parked_notes and (turn_done or not busy):
            notes, self._parked_notes = self._parked_notes, []
            self.engine._append({"role": "user", "content": "\n".join(notes)})

    def _unsent_pickups(self) -> None:
        """A pick-up message the user dropped from the queue told rocky nothing: its
        steps wait again, instead of vanishing with it."""
        queued = {item.prompt for item in self._delivery.pending}
        for text in [text for text in self._pickups if text not in queued]:
            self._approver.parked.extend(self._pickups.pop(text))
        self._render_park_chip()

    @staticmethod
    def _parked_call(step) -> str:
        """A parked step as the model must repeat it: the tool and its exact arguments."""
        return f"{step.tool} {json.dumps(step.args, ensure_ascii=False)}"

    @work
    async def open_parked(self) -> None:
        """Decide the parked steps, one card each: run it now · don't run it ·
        decide later. Opened by the chip, the turn's line, or /parked. An allowed
        step gets a pass for exactly that call, and rocky is told to pick it up:
        at once when idle, after the running turn otherwise."""
        from rockycode.tui.permission import InlineApproval, PermissionPicker

        if len(self.query(InlineApproval)) or len(self.query(PermissionPicker)):
            return
        try:
            self._transcript()
        except NoMatches:
            return
        approver = self._approver
        if not approver.parked:
            await self._add(Static(self._t('parked.none'), classes="tool-line"))
            return
        allowed, skipped = [], []
        for step in list(approver.parked):
            choice = await self._ask_inline(step.tool, step.detail, step.risk, step.warning, parked_at=step.at)
            if choice not in ("once", "deny"):
                break  # decide later: this one and the rest stay parked
            approver.settle(step)
            (allowed if choice == "once" else skipped).append(step)
            shown = escape(" ".join(step.detail.split())[:160])
            await self._add(Static(
                self._t('parked.allowed' if choice == "once" else 'parked.skipped', detail=shown), classes="tool-line"))
            self._render_park_chip()
        not_run = [self._t('parked.not_run_note', step=self._parked_call(step)) for step in skipped]
        if allowed:
            text = self._t('parked.pickup_one' if len(allowed) == 1 else 'parked.pickup_many', n=len(allowed))
            text = "\n".join([text, *(self._parked_call(step) for step in allowed), *not_run])
            # The pass is armed when this message's turn starts (_run_delivery), and only then:
            # a message that never runs allows nothing.
            self._pickups[text] = allowed
            if await self._submit_delivery(text, [], "queue") is None:  # waits for a running turn; never interrupts it
                self._pickups.pop(text, None)
                approver.parked.extend(allowed)  # rocky was not told, so the steps wait again
                self._render_park_chip()
        elif not_run:
            self._parked_notes.extend(not_run)
            self._tell_not_run()

    async def _ask_inline(
        self, tool: str, detail: str, risk: str, warning: str | None,
        session_label: str | None = None, parked_at: float | None = None, invited: bool = False,
    ) -> str:
        """Mount an InlineApproval just above the input and await the user's
        choice. Docking it into the chat (rather than a modal that owns the
        screen) keeps the transcript visible and scrollable — pageup / shift+↑↓
        / the wheel all still work while the prompt waits. Returns one of
        'once' | 'session' | 'deny' | 'cancel' (a parked step: 'once' | 'deny' |
        'later')."""
        from rockycode.tui.permission import InlineApproval

        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        widget = InlineApproval(tool, detail, risk, warning, fut, session_label=session_label,
                                language=self._language, parked_at=parked_at, invited=invited)
        await self.mount(widget, before=self.query_one("#prompt"))
        try:
            return await fut
        finally:
            # The keyboard goes back to the input, so the user can keep typing / scrolling —
            # unless they already took it elsewhere. A card that still waits does not get it.
            try:
                cardkeys.card_closing(self, widget)
            except Exception:  # noqa: BLE001 — app may be tearing down
                pass
            if widget.is_mounted:
                await gone(widget)
            # The card resized the transcript on its way in and out; it can come back one row
            # short of its end, with its last line (a place to click) under the border.
            self.call_after_refresh(self._back_to_the_end)

    def _tool_detail(self, name: str, args: dict) -> str:
        return tool_detail(name, args)

    def _permission_note(self) -> str:
        mode = self._permission_mode if self._permission_mode in ("yolo", "careful") else "ask"
        return self._t("app.note." + mode)

    def _perm_chip(self) -> str:
        """The always-visible status-bar permission indicator (red for yolo)."""
        if self._permission_mode == "yolo":
            return f"[{RED}]🔓 YOLO[/]"
        if self._permission_mode == "careful":
            label = self._t("app.careful")
        else:
            label = self._t("app.ask")
        return f"[{MUTED}]🔒 {label}[/]"

    def _render_perm_chip(self) -> None:
        """Repaint the approval badge. It carries its own key hint (shift+tab)
        and a hover tooltip, so neither the mode nor the way to change it is
        ever something you have to remember."""
        try:
            chip = self.query_one("#permchip", PermChip)
        except NoMatches:
            return  # a screen is on top of chat
        chip.update(f"{self._perm_chip()} [dim]· shift+tab[/]")
        chip.tooltip = self._t("app.perm_tooltip")

    def _set_permission_mode(self, mode: str) -> None:
        """Flip the approval mode for this session (does NOT persist — use /config
        for that). Tightening clears the session allowlist so tools previously
        OK'd via 'allow all' will ask again; loosening leaves it untouched."""
        rank = {"yolo": 0, "ask": 1, "careful": 2}
        if rank[mode] > rank[self._permission_mode]:
            self._approver.session.clear()  # tightening revokes bash binary grants too
            self.engine.read_grants.clear()  # and the session's widened read scopes
            self._approver.once.clear()     # and a pass a pick-up turn holds right now
        self._permission_mode = mode
        self._render_cwd()

    def _resume_paused_loops(self) -> int:
        """Loops paused FOR AN APPROVAL back to running, due now — yolo means
        there is nothing left to approve. A manual /loop pause is the user's
        own decision and stays paused (LoopBook.resume_blocked)."""
        return len(self._loops.resume_blocked())

    async def _apply_permission_switch(self, mode: str) -> None:
        """Flip the mode, say so in the transcript, re-run the yolo-on-host
        nudge. Shared by shift+tab, the chip picker and /permission, so the
        three routes can't drift apart — whichever one you take, the transcript
        records the switch and names the other two.

        Landing on yolo resumes every loop paused for an approval. Leaving
        them paused would stick the next check: yolo allows ask-tier, so there
        is nothing left to approve. A manual /loop pause stays. The receipt
        says how many came back."""
        self._set_permission_mode(mode)
        label = self._t("app.permission")
        hint = self._t("app.perm_hint")
        await self._add(Static(
            f"[{VIOLET}]✦ {label} → {self._permission_note()}[/] [dim]({hint})[/]",
            classes="tool-line"))
        if mode == "yolo":
            n = self._resume_paused_loops()
            if n:
                noun = "loop" if n == 1 else "loops"
                await self._add(Static(
                    self._t('app.loops_resumed_by_yolo', p1=n, p2=noun),
                    classes="tool-line"))
                self._loop_pump()
        await self._maybe_nudge_yolo_host()

    async def action_cycle_permission(self) -> None:
        """shift+tab: step the approval mode one notch looser (careful → ask →
        yolo, then back to careful) for this session — the same switch
        /permission makes, minus the typing.

        No-ops while an approval prompt or the picker is waiting (changing the
        rules under a question you're mid-answer to is nonsense) and while a
        screen sits on top of chat, where the transcript isn't even mounted."""
        from rockycode.engine.permission import next_mode
        from rockycode.tui.permission import InlineApproval, PermissionPicker

        if len(self.query(InlineApproval)) or len(self.query(PermissionPicker)):
            return
        try:
            self._transcript()
        except NoMatches:
            return
        await self._apply_permission_switch(next_mode(self._permission_mode))

    @work
    async def open_permission_picker(self) -> None:
        """The clickable route to the same switch: an inline card listing all
        three modes and what each one actually allows. Opened by clicking the
        status-bar chip or by a bare /permission. Runs as a worker so a click
        handler can start it without blocking the message pump."""
        from rockycode.tui.permission import InlineApproval, PermissionPicker

        if len(self.query(PermissionPicker)) or len(self.query(InlineApproval)):
            return
        try:
            self._transcript()
        except NoMatches:
            return
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        widget = PermissionPicker(self._permission_mode, fut, language=self._language)
        await self.mount(widget, before=self.query_one("#prompt"))
        try:
            choice = await fut
        finally:
            if widget.is_mounted:
                await gone(widget)
            try:  # give focus back to the input so typing just works again
                self.query_one(ChatInput).focus()
            except Exception:  # noqa: BLE001 — app may be tearing down
                pass
        if choice and choice != self._permission_mode:
            await self._apply_permission_switch(choice)

    def _in_sandbox(self) -> bool:
        return self.sandbox is not None and self.sandbox.is_running

    async def _maybe_nudge_yolo_host(self) -> None:
        """When yolo is on but tools run on the real machine (no sandbox), say
        plainly what that means and point at /sandbox to contain it. Only fires in
        the exposed state, so it's quiet once a sandbox is on or the mode tightens."""
        if self._permission_mode != "yolo" or self._in_sandbox():
            return
        await self._add(Static(self._t("app.yolo_host_warning"), classes="tool-line"))
