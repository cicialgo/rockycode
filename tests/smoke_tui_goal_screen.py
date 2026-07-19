"""In-app goal screen (headless Textual pilot, no Docker, no LLM).

Drives the real GoalScreen state machine with a FAKE backend, proving the UX the
screen owns — plan → confirm(y/e/n) → milestone events → summary → back to chat —
without any of the workspace / sandbox / model machinery (that lives behind the
GoalBackend seam and is exercised by the goal driver/runner smokes). Covers:
  - y at the gate  → runs, streams events, shows summary, Enter returns it
  - e at the gate  → discuss → the plan is revised, then y runs the new plan
  - n at the gate  → cancels, backend cleaned up (keep=False), 'cancelled' summary
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.chdir(tempfile.mkdtemp(prefix="rockygoalscreen-"))

from textual.app import App

from rockycode.engine.goal_session import GoalSummary, Permits
from rockycode.tui.goal_screen import GoalScreen


class FakeBackend:
    """Canned goal backend — records what the screen asked it to do."""

    def __init__(self, *, revise=False):
        self.revise = revise
        self.setup_called = False
        self.discussed = []
        self.ran_plan = None
        self.cleaned = None          # keep flag from cleanup(), or None if never
        self.events = ["[1] working: first", "[1] verified: first", "[1] committed",
                       "[2] working: second", "[2] verified: second"]

    async def setup(self):
        self.setup_called = True
        return "/tmp/ws-abc  ·  branch goal/demo"

    async def plan(self):
        return (["Add a docstring", "Run the tests"], "")

    def permits(self, plan, requires):
        return Permits(use_network=False, net_reason="", asks=[], approved=set(), blocked=None)

    async def discuss(self, plan, requires, msg):
        self.discussed.append(msg)
        return ("Good point — no network needed, revised.", ["Add a docstring (concise)", "Run the tests"], "")

    async def run(self, plan, permits, on_event):
        self.ran_plan = list(plan)
        for e in self.events:
            on_event(e)
        return GoalSummary("done", "all milestones complete", 2, 2,
                           branch="goal/demo", origin="/repo", workspace="/tmp/ws-abc",
                           log="/home/u/.rockycode/goal-logs/demo.log", currency="usd", spend=0.0123)

    async def cleanup(self, keep):
        self.cleaned = keep


class BlockingBackend(FakeBackend):
    """run() emits one event then blocks forever — to test Esc-while-running."""

    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()

    async def run(self, plan, permits, on_event):
        self.ran_plan = list(plan)
        on_event("[1] working: first")
        self.started.set()
        await asyncio.Event().wait()   # never completes; only a cancel ends it


class Host(App):
    """Minimal host that pushes the goal screen and captures its dismiss value."""

    def __init__(self, backend):
        super().__init__()
        self._backend = backend
        self.result = "UNSET"

    def on_mount(self):
        self.push_screen(GoalScreen(self._backend, "make it nice"), callback=self._done)

    def _done(self, summary):
        self.result = summary


async def wait_state(pilot, screen, state, timeout=5.0, step=0.05):
    for _ in range(int(timeout / step)):
        await pilot.pause(step)
        if screen._state == state:
            return True
    return screen._state == state


def goal_screen(app):
    return next(s for s in app.screen_stack if isinstance(s, GoalScreen))


async def main():
    # --- y: approve the plan → run → summary → Enter returns it ---
    be = FakeBackend()
    app = Host(be)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        gs = goal_screen(app)
        assert await wait_state(pilot, gs, "confirm"), f"should reach the plan gate, at {gs._state}"
        assert be.setup_called, "backend.setup must run"
        await pilot.press("y")
        assert await wait_state(pilot, gs, "done"), f"should finish, at {gs._state}"
        assert be.ran_plan == ["Add a docstring", "Run the tests"], be.ran_plan
        # every milestone event must be rendered BEFORE the summary (no interleave)
        lines = [str(w.render()) for w in gs.query("#goal-log Static")]
        done_i = next(i for i, l in enumerate(lines) if "goal done" in l)
        ev_i = [i for i, l in enumerate(lines) if "working:" in l or "verified:" in l or "committed" in l]
        assert ev_i and all(i < done_i for i in ev_i), f"events must precede the summary: done@{done_i} events@{ev_i}"
        # the summary must warn that the work is in a SEPARATE worktree, and point
        # back to chat (review/merge happen there — no separate screen mode)
        assert any("separate" in l and "worktree" in l for l in lines), "missing the worktree reminder"
        assert any("back to chat" in l for l in lines), "missing the back-to-chat pointer"
        assert app.result == "UNSET", "must not return until the user presses Enter"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert isinstance(app.result, GoalSummary) and app.result.status == "done", app.result
        assert app.result.milestones_done == 2

    # --- e: discuss → plan revised → y runs the REVISED plan ---
    be = FakeBackend()
    app = Host(be)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        gs = goal_screen(app)
        assert await wait_state(pilot, gs, "confirm"), gs._state
        await pilot.press("e")
        assert await wait_state(pilot, gs, "edit"), f"e should open the edit input, at {gs._state}"
        from textual.widgets import Input
        box = gs.query_one("#goal-edit", Input)
        box.value = "does this need pip install?"
        await pilot.press("enter")                    # submit the discuss note
        assert await wait_state(pilot, gs, "confirm"), "should return to the gate after discuss"
        assert be.discussed == ["does this need pip install?"], be.discussed
        # YOUR question must be echoed in the log (not just rocky's answer)
        assert any("does this need pip install?" in str(w.render()) for w in gs.query("#goal-log Static")), \
            "the user's discuss question must be shown above rocky's answer"
        await pilot.press("y")
        assert await wait_state(pilot, gs, "done"), gs._state
        assert be.ran_plan == ["Add a docstring (concise)", "Run the tests"], "must run the REVISED plan"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert app.result.status == "done"

    # --- n: cancel at the gate → cleaned up (keep=False), 'cancelled' summary ---
    be = FakeBackend()
    app = Host(be)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        gs = goal_screen(app)
        assert await wait_state(pilot, gs, "confirm"), gs._state
        await pilot.press("n")
        assert await wait_state(pilot, gs, "done"), gs._state
        assert be.cleaned is False, "cancel must clean up the unstarted workspace (keep=False)"
        assert be.ran_plan is None, "cancel must not run anything"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert app.result.status == "cancelled", app.result

    # --- esc while running → stop, keep partial work (keep=True), 'stopped' ---
    be = BlockingBackend()
    app = Host(be)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        gs = goal_screen(app)
        assert await wait_state(pilot, gs, "confirm"), gs._state
        await pilot.press("y")
        assert await wait_until(pilot, lambda: gs._state == "running" and be.started.is_set()), \
            f"should be running, at {gs._state}"
        await pilot.press("escape")                   # stop the run
        assert await wait_state(pilot, gs, "done"), f"esc should stop → done, at {gs._state}"
        assert be.cleaned is True, "stopping mid-run must KEEP the branch (committed milestones)"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert app.result.status == "stopped", app.result

    # --- full wiring: /goal in the real app → screen → back to chat + context seed ---
    from rockycode.engine.loop import Engine
    from rockycode.tui.app import ChatInput, RockyCodeApp

    be = FakeBackend()
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace()))
    eng = Engine(model="fake", client=client, workdir=Path.cwd())
    app = RockyCodeApp(eng, permission="yolo")

    async def _docker_yes():
        return True

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        app._docker_ready = _docker_yes                      # pretend Docker is up
        app._make_goal_backend = lambda obj: be              # inject the fake backend
        inp = app.query_one(ChatInput)
        inp.focus()
        inp.text = "/goal make the readme nicer"
        await pilot.press("enter")
        # the goal screen should push on top of chat
        assert await wait_until(pilot, lambda: any(isinstance(s, GoalScreen) for s in app.screen_stack)), \
            "/goal should open the goal screen"
        gs = goal_screen(app)
        assert await wait_state(pilot, gs, "confirm"), gs._state
        await pilot.press("y")
        assert await wait_state(pilot, gs, "done"), gs._state
        await pilot.press("enter")                            # back to chat
        assert await wait_until(pilot, lambda: not any(isinstance(s, GoalScreen) for s in app.screen_stack)), \
            "should pop back to chat"
        # the model got a context seed so 'review it' / 'keep going' work
        assert await wait_until(pilot, lambda: any(
            m.get("role") == "user" and "autonomous goal" in str(m.get("content", ""))
            for m in eng.history)), "chat history should carry the goal recap for the model"

    print("TUI GOAL-SCREEN SMOKE OK — plan gate y/e/n · discuss revises · events · summary · back to chat · /goal wiring. amaze!")


async def wait_until(pilot, cond, timeout=5.0, step=0.05):
    for _ in range(int(timeout / step)):
        await pilot.pause(step)
        if cond():
            return True
    return bool(cond())


asyncio.run(main())
