"""The in-app goal screen.

/goal runs the autonomous agent in its OWN full-screen Textual screen, pushed on
top of the chat — plan, confirm, milestones and the summary all render here in
the palette, and when it's done we pop back to chat with a one-line recap. No
more dropping to a bare terminal mid-session.

The screen only renders and collects input (y/e/n at the gate, a line of text to
discuss/edit the plan, Esc to stop). All the real work — git worktree, Docker
sandbox, the milestone loop — lives behind the GoalBackend seam
(engine/goal_session.py), so this file has no idea Docker exists and is testable
with a fake backend.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from rich.markup import escape
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Input, Static

from rockycode.goal.session import GoalSummary, Permits
from rockycode.i18n import tr, resolve_locale
from rockycode.palette import AMBER, LAVENDER, MUTED, PURPLE, RED, VIOLET

_ROCKY = "#8b6fc9"


class GoalScreen(Screen):
    """Drive one goal run to completion, then dismiss(GoalSummary).

    States: setup → planning → confirm ⇄ edit → running → done. `backend` is any
    object with setup()/plan()/permits()/discuss()/run()/cleanup() — LiveGoalBackend
    in production, a fake in tests."""

    CSS = """
    GoalScreen { layout: vertical; background: $surface; }
    #goal-head {
        dock: top; height: 3; padding: 1 2;
        border-bottom: solid $primary;
    }
    #goal-log { height: 1fr; padding: 0 2; margin: 1 0; }
    #goal-log > Static { margin-bottom: 0; }
    #goal-foot {
        dock: bottom; height: auto; min-height: 1; padding: 0 2 1 2;
        border-top: solid $primary;
    }
    #goal-edit { margin: 1 0 0 0; display: none; }
    #goal-edit.on { display: block; }
    """

    BINDINGS = [
        Binding("y", "decide('go')", "run", show=False),
        Binding("e", "decide('edit')", "discuss/edit", show=False),
        Binding("n", "decide('cancel')", "cancel", show=False),
        Binding("escape", "stop", "stop", show=False),
        Binding("pageup", "log_up", "scroll", show=False, priority=True),
        Binding("pagedown", "log_down", "scroll", show=False, priority=True),
    ]

    def __init__(self, backend, objective: str, *, language: str = "en") -> None:
        self._language = resolve_locale(language)
        super().__init__()
        self._backend = backend
        self._objective = objective
        self._state = "setup"
        self._decision: Optional[asyncio.Future] = None
        self._edit: Optional[asyncio.Future] = None
        self._summary: Optional[GoalSummary] = None
        self._events: Optional[asyncio.Queue] = None
        self._consumer: Optional[asyncio.Task] = None

    # ---- layout -------------------------------------------------------------

    def compose(self):
        yield Static("", id="goal-head")
        yield VerticalScroll(id="goal-log")
        foot = Static("", id="goal-foot")
        yield foot
        edit = Input(placeholder=tr("widgets.goal.edit", self._language), id="goal-edit")
        yield edit

    def on_mount(self) -> None:
        self.query_one("#goal-log", VerticalScroll).can_focus = False
        self._render_head(tr("widgets.goal.starting", self._language))
        self._run()

    # ---- rendering ----------------------------------------------------------

    def _render_head(self, status: str) -> None:
        obj = escape(self._objective) if self._objective else tr("widgets.goal.empty", self._language)
        try:
            self.query_one("#goal-head", Static).update(
                tr("widgets.goal.heading", self._language, title_colour=VIOLET, colour=LAVENDER, status=status, objective=obj))
        except NoMatches:
            pass  # screen tearing down (app closing / popped) — nothing to paint

    def _foot(self, markup: str) -> None:
        try:
            self.query_one("#goal-foot", Static).update(markup)
        except NoMatches:
            pass

    async def _line(self, markup: str, *, literal: str | None = None) -> None:
        """Append one line to the log and keep it pinned to the bottom. A no-op if
        the screen is gone (worker still winding down after a pop / app close)."""
        try:
            log = self.query_one("#goal-log", VerticalScroll)
        except NoMatches:
            return
        w = Static(literal, markup=False) if literal is not None else Static(markup)
        await log.mount(w)
        log.scroll_end(animate=False)

    def _emit_event(self, msg: str) -> None:
        """on_event callback for the runner. It fires synchronously inside the run
        loop; queue the message so a single consumer renders events strictly in
        order (and so the summary can wait for the queue to drain before it prints,
        instead of racing the scheduled appends)."""
        if self._events is not None:
            self._events.put_nowait(msg)

    async def _consume_events(self) -> None:
        while True:
            msg = await self._events.get()
            await self._line(f"[{MUTED}]·[/] {escape(msg)}")
            self._events.task_done()

    # ---- the run ------------------------------------------------------------

    def _run(self):
        self.run_worker(self._drive(), exclusive=True, name="goal-drive")

    async def _drive(self) -> None:
        try:
            self._render_head(tr("widgets.goal.setup", self._language))
            where = await self._backend.setup()
            await self._line(tr("widgets.goal.workspace", self._language, colour=PURPLE, path=escape(where)))

            self._render_head(tr("widgets.goal.planning", self._language))
            self._foot(tr("widgets.goal.drafting", self._language, colour=LAVENDER, objective=escape(self._objective)))
            plan, requires = await self._backend.plan()
            if not plan:
                await self._fail("the planner produced no milestones.",
                                 display_reason=tr("widgets.goal.no_plan", self._language))
                return

            # confirm ⇄ edit loop (cheap — no sandbox yet)
            while True:
                permits = self._backend.permits(plan, requires)
                if permits.blocked:
                    await self._fail(f"plan names a blocked action: {permits.blocked}",
                                     display_reason=tr("widgets.goal.blocked", self._language, action=permits.blocked))
                    return
                await self._show_plan(plan, permits)
                choice = await self._await_decision()
                if choice == "go":
                    break
                if choice == "cancel":
                    await self._line(tr("widgets.goal.nothing_ran", self._language, colour=AMBER))
                    await self._backend.cleanup(keep=False)
                    self._finish(GoalSummary("cancelled", "cancelled at the plan gate"))
                    return
                # edit → discuss: echo YOUR question, rocky answers, then we
                # re-show the (revised) plan.
                msg = await self._await_edit()
                if msg:
                    await self._line(tr("widgets.goal.user_note", self._language, colour=LAVENDER, note=escape(msg)))
                    self._render_head(tr("widgets.goal.thinking", self._language))
                    reply, plan, requires = await self._backend.discuss(plan, requires, msg)
                    if reply:
                        await self._line(f"[b {_ROCKY}]rocky:[/] [italic]{escape(reply)}[/]")

            # run — a single consumer renders events in order; join() guarantees
            # every event is on screen before the summary prints.
            self._state = "running"          # so Esc now stops the run (not cancels a gate)
            self._render_head(tr("widgets.goal.running", self._language))
            self._foot(tr("widgets.goal.running_keys", self._language, colour=LAVENDER))
            await self._line(tr("widgets.goal.sandbox", self._language, colour=PURPLE, network=tr("widgets.on" if permits.use_network else "widgets.goal.offline", self._language)))
            self._events = asyncio.Queue()
            self._consumer = asyncio.create_task(self._consume_events())
            try:
                summary = await self._backend.run(plan, permits, on_event=self._emit_event)
                await self._events.join()
            finally:
                self._consumer.cancel()
            await self._show_summary(summary)
            self._finish(summary)
        except asyncio.CancelledError:
            # Esc while running: the runner commits each milestone, so partial work
            # is safe on the branch. Keep it for review.
            await self._backend.cleanup(keep=True)
            self._finish(GoalSummary("stopped", "stopped by you (finished milestones kept)"))
            raise
        except Exception as e:  # noqa: BLE001
            await self._fail(f"{type(e).__name__}: {e}")

    async def _fail(self, reason: str, *, display_reason: str | None = None) -> None:
        await self._line(f"[bold {RED}]✗ {escape(display_reason or reason)}[/]")
        await self._backend.cleanup(keep=False)
        self._finish(GoalSummary("error", reason))

    # ---- plan + summary panels ---------------------------------------------

    async def _show_plan(self, plan: list[str], permits: Permits) -> None:
        self._render_head(tr("widgets.goal.review", self._language, count=len(plan)))
        await self._line(tr("widgets.goal.plan_heading", self._language, colour=VIOLET, count=len(plan)))
        for i, m in enumerate(plan, 1):
            await self._line(f"  [{PURPLE}]{i}.[/] {escape(m)}")
        if permits.needs_notice:
            await self._line(tr("widgets.goal.notice", self._language))
            if permits.use_network:
                await self._line(tr("widgets.goal.network_notice", self._language, reason=escape(permits.net_reason or tr("widgets.goal.requested", self._language))))
            elif permits.net_reason:
                await self._line(tr("widgets.goal.network_forced_off", self._language, reason=escape(permits.net_reason)))
            for v in permits.asks:
                await self._line(f"  [{AMBER}]⬆ {escape(v.reason)}[/]")
        else:
            await self._line(tr("widgets.goal.no_permissions", self._language))
        self._foot(tr("widgets.goal.decision_keys", self._language, colour=LAVENDER))

    async def _show_summary(self, s: GoalSummary) -> None:
        sym = "¥" if s.currency == "cny" else "$"
        colour = {"done": VIOLET}.get(s.status, AMBER if s.status in ("budget", "stalled", "stopped") else RED)
        await self._line(tr("widgets.goal.summary", self._language, colour=colour, status=tr("widgets.goal.status." + s.status, self._language) if s.status in ("done", "budget", "stalled", "stopped", "error", "cancelled") else escape(s.status), reason=escape(s.reason)))
        await self._line(tr("widgets.goal.spend", self._language, done=s.milestones_done, total=s.milestones_total, symbol=sym, spend=s.spend))
        if s.branch:
            # The single most important thing to say: this did NOT touch their files.
            await self._line(
                tr("widgets.goal.branch_notice", self._language, colour=AMBER, branch=escape(s.branch)))
            await self._line(tr("widgets.goal.review_command", self._language, colour=LAVENDER, command=escape(s.review_cmd)))
            await self._line(tr("widgets.goal.run_command", self._language, colour=LAVENDER, path=escape(s.workspace)))
            await self._line(tr("widgets.goal.tidy_command", self._language, colour=LAVENDER, command=escape(s.tidy_cmd)))
        elif s.workspace:
            await self._line(tr("widgets.goal.review_workspace", self._language, colour=LAVENDER, path=escape(s.workspace)))
        if s.log:
            await self._line(tr("widgets.goal.log_path", self._language, colour=LAVENDER, path=escape(s.log)))
        if s.branch:
            await self._line(
                tr("widgets.goal.back", self._language))

    # ---- input plumbing -----------------------------------------------------

    async def _await_decision(self) -> str:
        self._decision = asyncio.get_running_loop().create_future()
        self._state = "confirm"
        self.focus()
        return await self._decision

    async def _await_edit(self) -> str:
        self._edit = asyncio.get_running_loop().create_future()
        self._state = "edit"
        box = self.query_one("#goal-edit", Input)
        box.add_class("on")
        box.value = ""
        box.focus()
        self._foot(tr("widgets.goal.edit_keys", self._language, colour=LAVENDER))
        try:
            return await self._edit
        finally:
            box.remove_class("on")
            self._state = "confirm"

    def action_decide(self, choice: str) -> None:
        if self._state == "confirm" and self._decision and not self._decision.done():
            self._decision.set_result(choice)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "goal-edit" and self._edit and not self._edit.done():
            self._edit.set_result(event.value.strip())
            event.stop()

    def action_stop(self) -> None:
        """Esc: at the gate it cancels; while running it stops the worker (partial
        work stays committed on the branch)."""
        if self._state in ("confirm",) and self._decision and not self._decision.done():
            self._decision.set_result("cancel")
        elif self._state == "edit" and self._edit and not self._edit.done():
            self._edit.set_result("")
        elif self._state == "running":
            w = next((w for w in self.workers if w.name == "goal-drive"), None)
            if w is not None:
                w.cancel()

    def action_log_up(self) -> None:
        self.query_one("#goal-log", VerticalScroll).scroll_page_up(animate=False)

    def action_log_down(self) -> None:
        self.query_one("#goal-log", VerticalScroll).scroll_page_down(animate=False)

    # ---- finish -------------------------------------------------------------

    def _finish(self, summary: GoalSummary) -> None:
        """Land in the done state — one clear exit (Enter → chat). Reviewing and
        merging happen back in chat (ask rocky, or run the commands shown), so
        there's no separate screen 'mode' to get stuck in."""
        self._summary = summary
        self._state = "done"
        self._render_head(tr("widgets.goal.status_label", self._language, status=tr("widgets.goal.status." + summary.status, self._language) if summary.status in ("done", "budget", "stalled", "stopped", "error", "cancelled") else summary.status))
        self._foot(tr("widgets.goal.done_keys", self._language, colour=LAVENDER))
        try:
            self.focus()
        except Exception:  # noqa: BLE001 — screen may be tearing down
            pass

    def on_key(self, event) -> None:
        # In the done state, Enter (or Esc) is the single way out — back to chat.
        if self._state == "done" and event.key in ("enter", "escape"):
            event.stop()
            self.dismiss(self._summary)
