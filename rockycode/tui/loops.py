"""Loops (/loop — engine/cron.py): the pump, ticks, the loop card verbs.

Part of the TUI shell (rockycode/tui/app.py), split out so each concern reads on its
own; the app class mixes this in and the methods keep `self` = the app.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from rich.markup import escape
from textual import work
from textual.widgets import Static
from textual.worker import get_current_worker

from rockycode.engine import AgentState
from rockycode.engine import cron as cron_mod

from rockycode.palette import AMBER
from rockycode.tui import cardkeys
from rockycode.tui.chatinput import ChatInput
from rockycode.tui.images import ImageRouteChoice
from rockycode.tui.removal import gone


def _last_reply(history: list[dict], start: int) -> str:
    """The final plain assistant message of the turn that began at history[start]."""
    from rockycode.engine import images as images_mod
    for m in reversed(history[start:]):
        if m.get("role") == "assistant" and m.get("content") and not m.get("tool_calls"):
            return images_mod.content_text(m["content"])
    return ""


class LoopsMixin:
    def _sym(self) -> str:
        return "¥" if self._currency == "cny" else "$"

    def _idle_for_tick(self) -> bool:
        """A tick may start only when nobody — human or engine — is mid-anything:
        no turn running, no screen over the chat (goal, picker, modal), no inline
        card awaiting a human (approval, plan gate, routine/proposal/loop card,
        image route, exit sheet), no sandbox start in flight."""
        if self.engine.planning.active:
            return False
        if self._delivery.running or self._delivery.pending:
            return False
        w = self._turn_worker
        if w is not None and w.is_running:
            return False
        if len(self.screen_stack) > 1 or self._exiting or self._sandbox_starting:
            return False
        from rockycode.tui.loopcard import LoopCard
        from rockycode.tui.permission import InlineApproval, PermissionPicker
        from rockycode.tui.plangate import InlinePlanGate
        from rockycode.tui.proposalcard import ProposalCard
        from rockycode.tui.routinecard import RoutineCard
        for cls in (InlineApproval, InlinePlanGate, RoutineCard, ProposalCard, LoopCard,
                    ImageRouteChoice, PermissionPicker):
            # A parked step's card waits for nobody: no turn stands behind it, so it holds no tick back.
            if any(getattr(card, "_parked_at", None) is None for card in self.query(cls)):
                return False
        return True

    def _loop_pump(self) -> None:
        """1 Hz timer: start the oldest due loop's tick when the session is idle.
        Sync and cheap. The tick is a worker in the SAME group as user turns —
        a submit cancels it — and because the pump only starts one when idle, a
        tick can never cancel a user turn. Missed ticks never stack: due() hands
        a loop over once however long the session was busy."""
        if self._tick_loop is not None:
            return
        due = self._loops.due()
        if not due or not self._idle_for_tick():
            return
        self._turn_worker = self._run_tick(due[0])

    @work(group="turn", exclusive=True)
    async def _run_tick(self, loop) -> None:
        # Keep the tick active through folding and receipt rendering, not just
        # the engine turn. Those UI awaits are still part of this worker's turn.
        self._tick_loop, self._tick_blocked = loop, None
        try:
            await self._drive_tick(loop)
        finally:
            self._tick_loop = None
            self._render_cwd()

    async def _drive_tick(self, loop) -> None:
        """One tick = one ordinary turn on the session engine, with three twists:
        the marker rides the user turn (cron.tick_marker), the approver never
        prompts (_approve_tool), and a QUIET tick is folded away — its widgets
        replaced by one streak line and its messages rolled back out of live
        context (the trajectory keeps them). NOTE stays; DONE ends the loop; a
        denied ask pauses it unless the session is yolo by the time the tick
        settles (ask-tier then runs, so pausing would stick a resumed loop);
        a user-set budget stops it; otherwise maybe one muted running-cost
        reminder."""
        book = self._loops
        now = datetime.now()
        tick_no = book.begin_tick(loop)
        n_hist = len(self.engine.history)
        n_widgets = len(self._transcript().children)
        cost_before = self._ledger.cost(self._currency)
        header = Static(
            self._t('app.loop_tick', p1=cron_mod.clock_face(now), p2=loop.id, p3=tick_no, p4=now),
            classes="tool-line")
        await self._add(header)
        self._render_cwd()
        completed = cancelled = False
        try:
            await self._drive_turn(cron_mod.tick_marker(loop, tick_no, now))
            completed = True
        except asyncio.CancelledError:
            cancelled = True  # a submit (worker replaced) or Esc (same worker)
            raise
        except Exception as e:  # noqa: BLE001 — one tick must never take the app down
            try:
                await self._flush_buffers()
                await self._finalize_reply()
            except Exception:  # noqa: BLE001
                pass
            self._set_status(AgentState.IDLE)
            await self._add(Static(
                self._t('app.loop_tick_error', p0=loop.id, p1=tick_no, p2=escape(f'{type(e).__name__}: {e}')),
                classes="error-msg"))
        finally:
            if not completed:
                # Settle so the loop stays alive and fires again; nothing to fold.
                book.end_tick(loop, "cancelled" if cancelled else "error",
                              cost=max(0.0, self._ledger.cost(self._currency) - cost_before))
                if cancelled and get_current_worker() is self._turn_worker:
                    self._cancelled_tick = (loop, tick_no)  # Esc: _after_cancel says so
        if not completed:
            return

        cost = max(0.0, self._ledger.cost(self._currency) - cost_before)
        verdict, msg = cron_mod.parse_tick_reply(_last_reply(self.engine.history, n_hist))
        blocked, self._tick_blocked = self._tick_blocked, None
        reason = book.end_tick(loop, verdict, cost=cost, message=msg)
        at = datetime.now()
        face = cron_mod.clock_face(at)
        sym = self._sym()
        if verdict == cron_mod.QUIET and not blocked:
            # Fold: the widgets go, the messages leave LIVE context (trajectory
            # keeps every record), one streak line updates in place.
            for w in list(self._transcript().children)[n_widgets:]:
                await gone(w)
            self.engine.rollback(n_hist, reason=f"loop #{loop.id} quiet tick {tick_no}")
            text = self._t('app.loop_quiet_last', p1=face, p2=loop.id, p3=loop.quiet_streak, p4=at)
            line = self._quiet_lines.get(loop.id)
            if line is not None and line.is_mounted:
                line.update(text)
            else:
                line = Static(text, classes="tool-line")
                self._quiet_lines[loop.id] = line
                await self._add(line)
        else:
            self._quiet_lines.pop(loop.id, None)  # the next quiet starts a fresh streak line
            header.update(
                self._t('app.loop_tick_header', p1=face, p2=loop.id, p3=tick_no, p4=at, p5=verdict))
        # A denial recorded before a mid-tick switch to yolo must not pause:
        # yolo allows ask-tier, so the loop stays on its schedule. Block-tier
        # never sets `blocked` — that refusal already printed on its own.
        if blocked and loop.status == "running" and self._permission_mode != "yolo":
            book.pause(loop, blocked_on=blocked)
            await self._add(Static(
                self._t('app.loop_paused_approval', p1=face, p2=loop.id, p3=tick_no, p4=escape(str(blocked['detail'])[:160]), p7=loop.id, p9=loop.id),
                classes="tool-line"))
        elif verdict == cron_mod.DONE:
            await self._add(Static(
                self._t('app.loop_done', p1=face, p2=loop.id, p3=at)
                + (f" [dim]— {escape(msg)}[/]" if msg else "")
                + f" [dim]· {loop.ticks} tick(s) · {sym}{loop.spend:.2f}[/]", classes="tool-line"))
        elif reason:
            await self._add(Static(
                self._t('app.loop_stopped_tick_s', p1=face, p2=loop.id, p3=escape(reason), p4=loop.ticks, p5=sym, p6=loop.spend), classes="tool-line"))
        else:
            r = book.reminder_due(loop)
            if r is not None:
                iv = cron_mod.fmt_duration(loop.interval_s)
                await self._add(Static(
                    self._t('app.loop_list_row', p1=face, p2=loop.id, p3=cron_mod.fmt_duration(r['hours'] * 3600), p4=r['ticks'], p5=sym, p6=r['spend'], p7=iv, p8=sym), classes="tool-line"))

    async def _handle_loop(self, text: str) -> None:
        """/loop [<interval> [for <dur>] [x<N>] [max $<amt>] <prompt>] — or /loop
        (list) · /loop stop|pause|resume|allow [id]."""
        parts = text.split(maxsplit=1)
        rest = parts[1].strip() if len(parts) > 1 else ""
        book = self._loops
        if not rest or rest.lower() in ("list", "ls"):
            if book.live():
                self._review_loops()
            else:
                await self._add(Static(self._t("app.loop_help"), classes="tool-line"))
            return
        verb, _, arg = rest.partition(" ")
        verb = verb.lower()
        if verb in ("stop", "pause", "resume", "allow"):
            loop, err = book.resolve(arg.strip() or None)
            if loop is None:
                await self._add(Static(f"[{AMBER}]? {escape(err)}[/]", classes="tool-line"))
                return
            if verb == "allow":
                # awaits a human (the inline approval) → a worker, never the pump
                self.run_worker(self._loop_verb(loop, "allow"), group="loops", exclusive=True)
            else:
                await self._loop_verb(loop, verb)
            return
        spec, err = cron_mod.parse_loop_args(rest)
        if spec is None:
            await self._add(Static(f"[{AMBER}]? {escape(err)}[/]\n{self._t('app.loop_help')}", classes="tool-line"))
            return
        loop = book.add(spec["prompt"], spec["interval_s"], for_s=spec["for_s"],
                        count=spec["count"], max_spend=spec["max_spend"])
        await self._on_loop_event("start", loop)

    async def _on_loop_event(self, kind: str, loop) -> None:
        """The receipt (a loop started — by /loop, or by rocky via loop_start) or
        the stop line (loop_stop). The book calls this for the tools; /loop calls
        it directly, so both paths read the same."""
        sym = self._sym()
        if kind == "start":
            tokens = self.engine._projected_prompt_tokens()
            est = cron_mod.tick_cost_estimate(
                tokens, self._ledger.rate(self.engine.model, self._currency))
            iv = cron_mod.fmt_duration(loop.interval_s)
            budget = loop.budget_note(sym)
            until = (self._t('app.loop_no_budget')
                     if budget == "no budget" else budget)
            who = (self._t('app.rocky_set_it_up', p0=escape(loop.prompt[:100]))
                   if loop.origin == "model" else "")
            perm = ("" if self._permission_mode == "yolo" else
                    self._t('app.loop_ticks_never_prompt', p2=self._permission_mode))
            await self._add(Static(
                self._t('app.loop_started', p1=cron_mod.clock_face(datetime.now()), p2=loop.id, p3=who, p4=iv, p5=until, p6=sym, p7=est, p8=tokens, p9=iv, p10=perm),
                classes="tool-line"))
            if budget == "no budget" and not self._loop_hinted:
                self._loop_hinted = True
                await self._add(Static(
                    self._t('app.loop_ceiling_hint', p1=iv, p2=iv, p3=sym, p4=iv), classes="tool-line"))
            await self._maybe_nudge_yolo_host()
        elif kind == "stop":
            await self._add(Static(
                self._t('app.loop_stopped_by_rocky_tick_s', p1=loop.id, p2=loop.ticks, p3=sym, p4=loop.spend), classes="tool-line"))
        self._render_cwd()

    @work(group="loops", exclusive=True, exit_on_error=False)
    async def _review_loops(self) -> None:
        """/loop with loops live: one inline card per loop — pause/resume · stop ·
        decide a pending approval · leave it. Esc stops the walk."""
        from rockycode.tui.loopcard import LoopCard

        for loop in list(self._loops.live()):
            fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            card = LoopCard(loop, self._sym(), fut, language=self._language)
            await self.mount(card, before=self.query_one("#prompt"))
            try:
                choice = await fut
            finally:
                cardkeys.card_closing(self, card)
                if card.is_mounted:
                    await gone(card)
            if choice == "close":
                break
            await self._loop_verb(loop, choice)
        self._render_cwd()
        try:
            self.query_one(ChatInput).focus()
        except Exception:  # noqa: BLE001 — focus is best-effort
            pass

    async def _loop_verb(self, loop, verb: str) -> None:
        """stop · pause · resume · allow on one loop — shared by /loop <verb> and
        the card. `allow` awaits a human, so callers run it in a worker."""
        book, sym = self._loops, self._sym()
        if verb == "stop":
            book.stop(loop, "stopped by you")
            await self._add(Static(
                self._t('app.loop_stopped', p1=loop.id, p2=loop.ticks, p3=sym, p4=loop.spend),
                classes="tool-line"))
        elif verb == "pause":
            if loop.status == "running":
                book.pause(loop)
                await self._add(Static(
                    self._t('app.loop_paused_loop_resume_brings_it_back', p1=loop.id, p2=loop.id),
                    classes="tool-line"))
            else:
                await self._add(Static(self._t('app.loop_is_already_paused', p1=loop.id), classes="tool-line"))
        elif verb == "resume":
            if loop.status == "paused":
                book.resume(loop)
                await self._add(Static(
                    self._t('app.loop_resumed', p1=loop.id),
                    classes="tool-line"))
            else:
                await self._add(Static(self._t('app.loop_isn_t_paused', p1=loop.id), classes="tool-line"))
        elif verb == "allow":
            await self._loop_allow(loop)
        self._render_cwd()

    async def _loop_allow(self, loop) -> None:
        """A paused loop's recorded ask, decided NOW while you're here: `once`
        lets exactly that call run on the next tick; `session` grants the tool
        (bash: its binary) like any approval — session_grantable still applies,
        so a dangerous command can't be session-granted from here either; deny
        keeps the loop paused."""
        b = loop.blocked_on
        if loop.status != "paused" or not b:
            await self._add(Static(
                self._t('app.loop_isn_t_waiting_on_an_approval', p1=loop.id), classes="tool-line"))
            return
        from rockycode.engine.permission import command_binary, session_grantable

        name, args = b["tool"], b.get("args", {})
        session_label = None
        if session_grantable(name, args):
            if name == "bash":
                binary = command_binary(str(args.get("command", "")))
                if binary:
                    session_label = self._t('app.allow_for_this_session', p0=binary)
            else:
                session_label = self._t('app.allow_tool_for_session', p0=name)
        choice = await self._ask_inline(name, b["detail"], b.get("risk", "risky"),
                                        b.get("warning"), session_label=session_label, invited=True)
        if choice in ("once", "session"):
            if choice == "session":
                if name == "bash":
                    bn = command_binary(str(args.get("command", "")))
                    if bn:
                        self._auto_approve_bins.add(bn)
                else:
                    self._auto_approve.add(name)
            else:
                self._tick_once.add((name, b["detail"]))
            self._grant_read_if_escaping(name, args)
            self._loops.resume(loop)
            how = self._t('app.allowed_for_this_session') if choice == "session" else self._t('app.that_call_may_run_once')
            await self._add(Static(
                self._t('app.loop_resumed_retry', p1=loop.id, p2=how), classes="tool-line"))
        else:
            await self._add(Static(
                self._t('app.loop_stays_paused', p1=loop.id, p3=loop.id, p6=loop.id),
                classes="tool-line"))

    # ---- transcript helpers -------------------------------------------------
