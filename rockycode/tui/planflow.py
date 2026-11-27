"""Plan mode in the chat: /plan, the review card, approve → build here or run as a goal.

Part of the TUI shell (rockycode/tui/app.py), split out so each concern reads on its
own; the app class mixes this in and the methods keep `self` = the app.
"""
from __future__ import annotations

import asyncio

from rich.markup import escape
from textual import work
from textual.css.query import NoMatches
from textual.widgets import Static
from rockycode.tui import cardkeys
from rockycode.tui.chatinput import ChatInput
from rockycode.tui.removal import gone


class PlanMixin:
    async def _handle_plan(self, text: str) -> None:
        """One interactive planning mode, using the current conversation."""
        from rockycode.engine.planning import ENTRY_MESSAGE

        if self._turn_worker is not None and self._turn_worker.is_running:
            await self._add(Static(self._t('app.plan_turn_busy'),
                                   classes="tool-line"))
            return
        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        planning = self.engine.planning
        if arg.lower() == "off":
            planning.leave()
            self._render_cwd()
            await self._add(Static(self._t('app.plan_mode_off_draft_kept'), classes="tool-line"))
            return
        was_active = planning.active
        try:
            path = planning.start()
            if was_active and not arg:
                content, _ = planning.read()
                if content.strip():
                    planning.request_review()
                    self._turn_worker = self._review_plan()
                    return
        except (OSError, ValueError) as exc:
            await self._add(Static(escape(str(exc)), classes="error-msg"))
            return
        self._render_cwd()
        await self._add(Static(
            self._t('app.plan_mode_entered', p1=path.relative_to(self.engine.workdir.resolve())),
            classes="tool-line"))
        if arg:
            await self._add(Static(arg, classes="user-msg", markup=False))
            self._turn_worker = self._run_turn(arg)
        elif not was_active and any(m.get("role") == "user" for m in self.engine.history):
            self._turn_worker = self._run_turn(ENTRY_MESSAGE)
        else:
            await self._add(Static(self._t('app.tell_rocky_what_you_want_to_plan'), classes="tool-line"))
        self.query_one(ChatInput).focus()

    @work(group="turn", exclusive=True)
    async def _review_plan(self) -> None:
        await self._maybe_plan_gate(invited=True)  # the user typed /plan

    async def _maybe_plan_gate(self, invited: bool = False) -> None:
        """Only explicit submission opens review; draft writes never do."""
        from rockycode.tui.plangate import InlinePlanGate

        planning = self.engine.planning
        if planning.status != "review":
            return
        snapshot = planning.snapshot(include_text=True)
        revision = snapshot["revision"]
        if snapshot.get("error") or revision != snapshot.get("current_revision"):
            planning.continue_discussion()
            await self._add(Static(self._t('app.plan_changed'),
                                   classes="tool-line"))
            return
        rel = snapshot["path"]
        while True:
            fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            widget = InlinePlanGate(rel, snapshot["text"], fut, language=self._language, invited=invited)
            await self.mount(widget, before=self.query_one("#prompt"))
            try:
                choice = await fut
            finally:
                try:
                    cardkeys.card_closing(self, widget)
                except NoMatches:  # app is shutting down
                    pass
                if widget.is_mounted:
                    await gone(widget)
            if choice != "goal":
                break
            try:
                handed_off = await self._plan_to_goal(revision)
            except ValueError as exc:
                await self._add(Static(escape(str(exc)), classes="error-msg"))
                self._render_cwd()
                return
            if handed_off:
                self._render_cwd()
                return
            # A failed Docker preflight keeps this SAME reviewed plan read-only and
            # reopens its gate, so retry or build-here needs no new model turn. The card
            # that comes back follows the user's own choice, so it has the keyboard —
            # unless they started a message while the hand-off was tried.
            invited = not self.query_one(ChatInput).text
        try:
            if choice == "approve":
                self._plan_go = planning.approve(revision)
                await self._add(Static(
                    self._t('app.plan_approved'),
                    classes="tool-line"))
            elif choice == "exit":
                planning.leave()
                await self._add(Static(self._t('app.plan_mode_off_draft_kept'), classes="tool-line"))
            else:
                planning.continue_discussion()
                if choice == "discuss":
                    await self._add(Static(self._t('app.plan_keep_discussing'),
                                           classes="tool-line"))
        except ValueError as exc:
            await self._add(Static(escape(str(exc)), classes="error-msg"))
        self._render_cwd()

    async def _plan_to_goal(self, revision: str) -> bool:
        """Validate Docker and freeze the reviewed revision before leaving plan mode.
        False means the Docker preflight failed (missing executable or daemon
        down — one shared probe, see _docker_ready); the caller reopens the gate."""
        from rockycode.tui.goal_screen import GoalScreen

        planning = self.engine.planning
        if not await self._docker_ready():
            await self._add(Static(
                self._t('app.goal_needs_docker'),
                classes="tool-line"))
            return False
        text, current = planning.read()
        if current != revision:
            planning.continue_discussion()
            raise ValueError(self._t('app.plan_changed_short'))
        rel = planning.snapshot()["path"]
        backend = self._make_goal_backend(f"implement the approved plan ({rel})",
                                          plan_file=planning.path, plan_text=text)
        planning.approve(revision)
        self._render_cwd()
        try:
            summary = await self.push_screen_wait(GoalScreen(backend, f"plan: {rel}", language=self._language))
        except BaseException:
            planning.attach(planning.path)
            raise
        if summary is None or summary.status in {"cancelled", "error"}:
            planning.attach(planning.path)
        await self._on_goal_return(summary)
        return True
