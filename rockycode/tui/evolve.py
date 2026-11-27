"""Evolve v2 in the chat: the launch catch-up dream, the proposals and routines
inboxes, the first-run disclosure, and the exit-time final reflect.

Part of the TUI shell (rockycode/tui/app.py), split out so each concern reads on its
own; the app class mixes this in and the methods keep `self` = the app.
"""
from __future__ import annotations

import asyncio

from textual import work
from textual.widgets import Static
from textual.worker import WorkerCancelled, WorkerFailed

from rockycode.dream import reflect as reflect_mod
from rockycode.tui import cardkeys
from rockycode.tui.chatinput import ChatInput
from rockycode.tui.removal import gone

EXIT_STOP_TIMEOUT = 3.0  # seconds /exit waits for what still runs to stop; then it leaves anyway


class EvolveMixin:
    @work(group="dream", exclusive=True, exit_on_error=False)
    async def _dream_catchup(self) -> None:
        """Evolve v2 catch-up: at launch, quietly consolidate this project's
        un-dreamed past sessions on the SESSION'S OWN model and client — no
        new auth, no local stack. Cold-reflects stragglers, folds patterns,
        drafts proposals. Silent unless it actually dreamed: memory off /
        evolve off / lock held / nothing pending → no noise. The live session
        is excluded; it gets its turn after its final reflect."""
        from rockycode.dream.core import DreamRunner

        if self._evolve != "auto" or getattr(self.engine, "memory_store", None) is None:
            return
        index = None
        try:
            from rockycode.memory.index import MemoryIndex
            index = MemoryIndex(self.engine.memory_store)
            index.conn()
        except Exception:  # noqa: BLE001 — semantic index is optional
            index = None
        runner = DreamRunner(
            self.engine.workdir,
            client=self.engine.client, model=self.engine.model,
            exclude={self.engine.trajectory.session_id},
        )
        try:
            report = await runner.run(limit=5, index=index)
        except RuntimeError:
            return  # another dream holds the lock — it's being handled
        except Exception:  # noqa: BLE001 — a failed dream must never disturb chat
            return
        if report.sessions_digested:
            drafted = " · drafted a proposal, /proposals" if report.proposals_drafted else ""
            await self._add(Static(
                self._t('app.dream_done', p1=report.sessions_digested, p2=report.facts_added, p3=report.facts_updated, p4=drafted),
                classes="tool-line"))

    @work(group="proposals", exclusive=True, exit_on_error=False)
    async def _review_proposals(self) -> None:
        """Walk this project's pending dream proposals, one inline card each.
        Approve installs the skill globally; archive keeps the file; later
        stops the walk and leaves the rest pending. The card is the trust
        boundary — dream drafts, only a human installs."""
        from rockycode.dream.proposals import ProposalStore
        from rockycode.session import get_project
        from rockycode.tui.proposalcard import ProposalCard

        store = ProposalStore()
        pending = store.list(project_id=get_project(self.engine.workdir).id)
        if not pending:
            await self._add(Static(
                self._t('app.proposals_none'), classes="tool-line"))
            return
        for p in pending:
            fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            card = ProposalCard(p, fut, language=self._language)
            await self.mount(card, before=self.query_one("#prompt"))
            try:
                choice = await fut
            finally:
                cardkeys.card_closing(self, card)
                if card.is_mounted:
                    await gone(card)
            if choice == "install":
                installed = store.approve(p)
                if p.kind == "retire-skill":
                    await self._add(Static(
                        self._t('app.skill_retired', p1=p.pattern or p.name, p2=installed),
                        classes="tool-line"))
                elif p.kind == "routine":
                    verb = self._t('app.routine_verb_amend') if p.amends else self._t('app.routine_verb_click')
                    await self._add(Static(
                        self._t('app.routine_installed', p1=p.name, p2=installed.parent, p3=verb),
                        classes="tool-line"))
                else:
                    await self._add(Static(
                        self._t('app.skill_installed', p1=p.name, p2=installed.parent),
                        classes="tool-line"))
            elif choice == "archive":
                store.archive(p)
                # The verdict feeds back (evolve v2): "no" decays the pattern
                # that drafted this, so it must re-earn hotness before asking
                # again — rocky learns the user's taste in proposals.
                decayed = ""
                mem_store = getattr(self.engine, "memory_store", None)
                if p.pattern and mem_store is not None:
                    from rockycode.dream.patterns import decay_pattern
                    if decay_pattern(mem_store, p.pattern):
                        decayed = " · rocky will ask for stronger evidence next time"
                await self._add(Static(
                    self._t('app.archived_kept_under_proposals_archived', p1=p.name)
                    + (self._t('app.archive_decayed_suffix') if decayed else ''),
                    classes="tool-line"))
            else:  # later — stop walking, leave the rest pending
                await self._add(Static(
                    self._t('app.later_proposals_when_you_re_ready'), classes="tool-line"))
                break
        try:
            self.query_one(ChatInput).focus()
        except Exception:  # noqa: BLE001 — focus is best-effort
            pass

    @work(group="routines", exclusive=False, exit_on_error=False)
    async def _run_routine_bg(self, r) -> None:
        """Run one routine detached (sandboxed exec) and report a single line.
        Docker down / any failure = a loud line, never a broken chat."""
        from rockycode.routines import RoutineStore, run_routine

        store = RoutineStore()
        await self._add(Static(
            self._t('app.routine_running_sandboxed', p1=r.name), classes="tool-line"))
        try:
            res = await run_routine(store, r, model=self.engine.model,
                                    client=self.engine.client)
        except Exception as e:  # noqa: BLE001
            await self._add(Static(
                self._t('app.routine_failed', p1=r.name, p2=e), classes="tool-line"))
            return
        where = r.output_dir or self._t('app.last_run_md_beside_the_routine')
        await self._add(Static(
            self._t('app.routine_see', p1=r.name, p2=res['status'], p3=res['cost'], p4=where, p5=res['session']), classes="tool-line"))

    @work(group="routines", exclusive=True, exit_on_error=False)
    async def _review_routines(self) -> None:
        """Walk due routines, one inline card each: run / lease / later / off."""
        from rockycode.routines import RoutineStore
        from rockycode.session import get_project
        from rockycode.tui.routinecard import RoutineCard

        store = RoutineStore()
        due = store.due(project_id=get_project(self.engine.workdir).id)
        if not due:
            await self._add(Static(
                self._t('app.routines_none_due'),
                classes="tool-line"))
            return
        for r in due:
            fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            card = RoutineCard(r, store.state(r).lease_spent, fut, language=self._language)
            await self.mount(card, before=self.query_one("#prompt"))
            try:
                choice = await fut
            finally:
                cardkeys.card_closing(self, card)
                if card.is_mounted:
                    await gone(card)
            if choice == "run":
                self._run_routine_bg(r)
            elif choice == "auto":
                store.grant_lease(r, days=7, budget=r.budget_lease)
                await self._add(Static(
                    self._t('app.routine_lease_granted', p1=r.name, p2=r.budget_lease),
                    classes="tool-line"))
                self._run_routine_bg(r)
            elif choice == "off":
                r.enabled = False
                store.save(r)
                await self._add(Static(
                    self._t('app.turned_off_re_enable_in_its_routine_toml', p1=r.name),
                    classes="tool-line"))
            else:  # later — stop walking
                await self._add(Static(
                    self._t('app.later_routines_when_you_re_ready'), classes="tool-line"))
                break
        try:
            self.query_one(ChatInput).focus()
        except Exception:  # noqa: BLE001 — focus is best-effort
            pass

    async def _maybe_evolve_disclosure(self) -> None:
        """Evolve bills the session model for a few capped reflect calls, so
        the first launch with `evolve = auto` (opt-in since 1.0.0; it shipped on
        from 2026-08-02 to 0.2.0) says so once — a marker in the global home
        keeps it to one line, ever. Best-effort: a read-only
        home never blocks the greeting."""
        from rockycode.session import HOME_ROOT
        marker = HOME_ROOT / "evolve-disclosed"
        try:
            if marker.exists():
                return
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("shown\n", encoding="utf-8")
        except OSError:
            return
        await self._add(Static(self._t('app.evolve_disclosure'), classes="tool-line"))

    async def _stop_for_exit(self) -> None:
        """Stop what still runs: the delivery's turn and its queue, then a turn the
        delivery does not own (a loop tick, a /plan turn)."""
        await self._delivery.close()
        worker = self._turn_worker
        if worker is not None and worker.is_running:
            self._waiting_external = False
            worker.cancel()
            try:
                await worker.wait()
            except (WorkerCancelled, WorkerFailed):
                pass

    async def _exit_with_reflect(self) -> None:
        """Leave the app after the final reflect (evolve v2) — the one moment
        the whole conversation is still the provider's warm prefix, so the
        graded capture is nearly free. Bounded two ways: reflect_final's own
        timeout means exit never hangs, and a second /exit or ctrl+q leaves
        immediately (the dream cold-reflects the straggler later, so an
        impatient exit loses nothing forever)."""
        self.engine.loops_at_exit = len(self._loops.live())  # the exit card says so
        if self._exiting:
            self.exit(self._switch_target)  # second request = leave NOW
            return
        self._exiting = True
        # What still runs is stopped first, the way Esc stops it, while there is
        # still a transcript to draw on. Left to the teardown, a turn ended in
        # "Task exception was never retrieved" (found live by dy2012, #144).
        # Bounded: a tool that is slow to stop must not hold the exit. Over the
        # limit, everything is told once more and rocky leaves without waiting.
        stop = asyncio.ensure_future(self._stop_for_exit())
        stop.add_done_callback(lambda task: task.cancelled() or task.exception())  # however it ends, it was looked at
        await asyncio.wait({stop}, timeout=EXIT_STOP_TIMEOUT)
        if not stop.done():
            stop.cancel()
        worker = self._turn_worker
        if worker is not None and worker.is_running:
            worker.cancel()
        # Heuristic outcome first — the graded reflect record must land after
        # it (readers take the last outcome). Idempotent; cli's call no-ops.
        self.engine.finalize_outcome()
        hook = reflect_mod.attached(self.engine)
        if self._evolve == "auto" and hook is not None and hook.wants_final(self.engine):
            try:
                await self._add(Static(
                    self._t('app.noting_session_for_dream'),
                    classes="tool-line"))
                if self._left:
                    return  # a second /exit or ctrl+q has left meanwhile: no reflect after leaving
                # A task of its own, so that exit() can stop it in the steps before its
                # request goes out (Python 3.11 takes one step more than later ones).
                self._final_reflect = asyncio.ensure_future(self.engine.run_final_hooks())
                await self._final_reflect
            except asyncio.CancelledError:
                if self._left:
                    return
                raise
            except Exception:  # noqa: BLE001 — a failed reflect must never block exit
                pass
        self.exit(self._switch_target)  # a /task open target rides the normal exit

    async def _evolve_startup(self) -> None:
        """Launch-time inbox lines: proposals waiting, routines due. Catch-up,
        never a daemon; nothing here may break startup."""
        # Dream proposals waiting from earlier passes — one muted, clickable-by-
        # command line; the inbox never nags beyond this.
        try:
            from rockycode.dream.proposals import ProposalStore
            from rockycode.session import get_project
            n = len(ProposalStore().list(project_id=get_project(self.engine.workdir).id))
            self._inbox["proposals"] = n
            if n:
                await self._add(Static(
                    self._t('app.proposals_waiting', p1=n),
                    classes="tool-line"))
        except Exception:  # noqa: BLE001 — the inbox must never break startup
            pass
        # Due routines: leased ones run themselves (that's what the lease
        # bought); the rest get one muted line. Catch-up, never a daemon.
        try:
            from rockycode.routines import RoutineStore
            from rockycode.session import get_project
            rstore = RoutineStore()
            due = rstore.due(project_id=get_project(self.engine.workdir).id)
            self._inbox["routines"] = len(due)
            leased = [r for r in due if rstore.lease_active(r)]
            manual = [r for r in due if not rstore.lease_active(r)]
            if manual:
                names = ", ".join(r.name for r in manual)
                await self._add(Static(
                    self._t('app.routine_s_due_routines_to_run', p1=names),
                    classes="tool-line"))
            for r in leased:
                self._run_routine_bg(r)
        except Exception:  # noqa: BLE001 — routines must never break startup
            pass
