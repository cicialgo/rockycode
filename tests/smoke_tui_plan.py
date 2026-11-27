"""Plan mode in the TUI (headless Textual pilot, no API).

Drives the real RockyCodeApp through: /plan → the model drafts the plan file
(a write the read-only gate ALLOWS) → submit_plan opens the inline plan gate →
y approves → plan mode off + the synthetic implement turn is submitted. Also
checks /plan off and the plan chip.
"""
import asyncio
import json
import os
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.chdir(tempfile.mkdtemp(prefix="rockyplan-"))

from textual.containers import VerticalScroll
from textual.widgets import Markdown, Static

from rockycode.engine.loop import Engine
from rockycode.engine.tools import build_registry
from rockycode.tui.app import RockyCodeApp
from rockycode.tui.chatinput import ChatInput
from rockycode.tui.plangate import InlinePlanGate
from rockycode.tui import cardkeys

cardkeys.ENTER_GUARD = 0  # the half second is tested in smoke_tui_card_keys.py; here Enter answers a card at once


async def come_to_card(pilot, app):
    """A card the user did not open never takes the keyboard: Enter in the empty message box goes to it."""
    await pilot.press("enter")
    for _ in range(80):
        await pilot.pause(0.025)
        if isinstance(app.focused, cardkeys.CardKeys):
            return
    raise AssertionError("Enter in the empty message box did not bring the keyboard to the card")

# what the fake model writes as the plan (target["path"] is set after /plan)
target = {"path": None, "submit": True,
          "content": "## Phase 1 — setup\n- create foo.py\n" + "- check scope\n" * 25 +
                     "## Phase 2 — test\n- last verification step"}


class U:
    def model_dump(self):
        return {"prompt_tokens": 30, "completion_tokens": 6}


def chunk(content=None, tool_calls=None, usage=None):
    d = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(usage=usage, choices=[types.SimpleNamespace(delta=d)])


def tc(i, id_, name, args):
    return types.SimpleNamespace(index=i, id=id_, function=types.SimpleNamespace(name=name, arguments=args))


async def stream(chunks):
    from invariants import completed_chunks
    for c in completed_chunks(chunks):
        yield c


class FakeCompletions:
    """Draft, optionally submit for review, then answer without implementing."""
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1 and target["path"]:
            return stream([
                chunk(tool_calls=[tc(0, "w", "write_file",
                                     json.dumps({"path": target["path"], "content": target["content"]}))]),
                chunk(usage=U()),
            ])
        if self.calls == 2 and target["submit"]:
            return stream([chunk(tool_calls=[tc(0, "s", "submit_plan", "{}")]), chunk(usage=U())])
        return stream([chunk(content="done."), chunk(usage=U())])


def build():
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions()))
    eng = Engine(model="fake", client=client, workdir=Path.cwd(), registry=build_registry(Path.cwd()))
    return RockyCodeApp(eng, permission="yolo"), eng


def gate(app):
    q = app.query(InlinePlanGate)
    # A mounted node can be observed before on_mount/focus finishes.
    return q.first() if len(q) and q.first().is_mounted else None


async def wait_until(pilot, cond, timeout=6.0, step=0.05):
    for _ in range(int(timeout / step)):
        await pilot.pause(step)
        if cond():
            return True
    return bool(cond())


async def main():
    app, eng = build()
    async with app.run_test(size=(95, 34)) as pilot:
        await pilot.pause(0.1)
        # 1) /plan sets the plan file, shows the chip, engine goes read-only
        await app._handle_plan("/plan")
        assert eng.plan_file is not None, "/plan must set a plan file"
        target["path"] = str(eng.plan_file)
        assert "📋 plan" in str(app.query_one("#cwd", Static).render()), "plan chip missing"

        # 2) an explicit submit after drafting opens the full, scrollable plan
        inp = app.query_one(ChatInput)
        inp.focus()
        inp.text = "plan the hello feature"
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: gate(app) is not None), "plan gate should pop after the draft"
        assert eng.plan_file.read_text().strip(), "the plan file should have been written"
        assert "last verification step" in gate(app).query_one(Markdown)._markdown
        preview = gate(app).query_one(VerticalScroll)
        assert preview.max_scroll_y > 0, "long plans must scroll instead of truncating"
        await come_to_card(pilot, app)
        await pilot.press("pagedown")
        await pilot.pause()
        assert preview.scroll_y > 0

        # Enter defaults to discussion; unchanged content can be reviewed again.
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: gate(app) is None)
        assert eng.planning.active and eng.planning.status == "planning"
        await app._handle_plan("/plan")
        assert await wait_until(pilot, lambda: gate(app) is not None)

        # 3) approve → plan mode off + the implement turn is queued/submitted
        await pilot.press("down", "enter")
        assert await wait_until(pilot, lambda: eng.plan_file is None), "approve must leave plan mode"
        assert await wait_until(pilot, lambda: any(
            "plan approved" in str(w.render()) for w in app.query(Static))), "approval line missing"
        # the synthetic implement turn carries the plan path into history
        assert await wait_until(pilot, lambda: any(
            m.get("role") == "user" and "implement the plan" in str(m.get("content", ""))
            for m in eng.history)), "implement turn should be submitted"
        assert target["content"] in next(m["content"] for m in eng.history
                                         if m.get("role") == "user" and "Plan approved" in m["content"])

    # 4) /plan off leaves cleanly, keeping the file
    app, eng = build()
    async with app.run_test(size=(95, 34)) as pilot:
        await pilot.pause(0.1)
        await app._handle_plan("/plan")
        pf = eng.plan_file
        assert pf is not None
        await app._handle_plan("/plan off")
        assert eng.plan_file is None, "/plan off must clear plan mode"
        assert pf.exists(), "/plan off keeps the file for reference"
        assert "📋 plan" not in str(app.query_one("#cwd", Static).render()), "chip should clear"

    # 5) g → hand the approved plan to goal (Docker mocked); goal gets the plan file
    from rockycode.goal.session import GoalSummary, Permits
    from rockycode.tui.goal_screen import GoalScreen

    class FakeGoalBackend:
        async def setup(self):
            return "/ws · branch goal/x"

        async def plan(self):
            return (["Phase 1 — setup"], "")

        def permits(self, p, r):
            return Permits(use_network=False, net_reason="", asks=[], approved=set(), blocked=None)

        async def discuss(self, p, r, m):
            return ("", p, r)

        async def run(self, plan, permits, on_event):
            on_event("[1] working: Phase 1")
            return GoalSummary("done", "complete", 1, 1, branch="goal/x",
                               origin="/repo", workspace="/ws", log="/l")

        async def cleanup(self, keep):
            pass

    app, eng = build()
    captured = {}

    async def _docker_yes():
        return True

    async with app.run_test(size=(95, 34)) as pilot:
        await pilot.pause(0.1)
        await app._handle_plan("/plan")
        target["path"] = str(eng.plan_file)
        plan_path = eng.plan_file
        app._docker_ready = _docker_yes
        app._make_goal_backend = lambda obj, plan_file=None, plan_text=None: (
            captured.update(obj=obj, pf=plan_file, text=plan_text), FakeGoalBackend())[1]
        inp = app.query_one(ChatInput)
        inp.focus()
        inp.text = "plan it"
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: gate(app) is not None), "gate should pop"
        await come_to_card(pilot, app)
        await pilot.press("down", "down", "enter")   # run as goal
        assert await wait_until(pilot, lambda: any(isinstance(s, GoalScreen) for s in app.screen_stack)), \
            "run as goal should open the goal screen"
        # goal got the SAME approved plan file (skips its own planner)
        assert captured.get("pf") == plan_path, f"plan file not handed to goal: {captured}"
        assert captured["text"] == target["content"], "goal receives the reviewed snapshot"
        assert eng.plan_file is None, "handoff leaves plan mode"
        gs = next(s for s in app.screen_stack if isinstance(s, GoalScreen))
        assert await wait_until(pilot, lambda: gs._state == "confirm"), gs._state
        await pilot.press("y")   # confirm the goal run
        assert await wait_until(pilot, lambda: gs._state == "done"), gs._state
        await pilot.press("enter")   # back to chat
        assert await wait_until(pilot, lambda: not any(isinstance(s, GoalScreen) for s in app.screen_stack)), \
            "should return to chat"

    # Incremental drafts do not open review. Docker failure does not leave planning.
    from unittest.mock import AsyncMock, patch
    target["submit"] = False
    app, eng = build()
    async with app.run_test(size=(95, 34)) as pilot:
        await pilot.pause(0.1)
        await app._handle_plan("/plan")
        target["path"] = str(eng.plan_file)
        app._turn_worker = app._run_turn("first discuss the requirements")
        assert await wait_until(pilot, lambda: app._turn_worker.is_finished)
        assert gate(app) is None and eng.planning.status == "planning"
        assert eng.plan_file.read_text() == target["content"]
        await app._handle_plan("/plan")
        assert await wait_until(pilot, lambda: gate(app) is not None)
        # Missing executable AND a daemon that is down both reopen the SAME gate:
        # the reviewed plan stays read-only; no new model turn, no /goal re-plan.
        plan_bytes = eng.plan_file.read_bytes()
        app._docker_ready = AsyncMock(return_value=False)
        for executable in (None, "/fake/docker"):
            previous_gate = gate(app)
            with patch("shutil.which", return_value=executable):
                await pilot.press("down", "down", "enter")
                assert await wait_until(pilot, lambda: gate(app) is not None and gate(app) is not previous_gate), \
                    "a failed goal handoff must reopen the plan gate"
            assert eng.planning.active and eng.planning.status == "review", "failed handoff keeps the review"
            assert eng.plan_file.read_bytes() == plan_bytes, "failed handoff keeps the reviewed plan"
            assert not any(isinstance(s, GoalScreen) for s in app.screen_stack)
        assert app._docker_ready.await_count == 2, "both attempts must go through the readiness probe"
        assert any("Run as goal" in str(w.render()) for w in app.query(Static)), "guidance names the gate choices"
        # The card that comes back has the keyboard, because the user's own choice brought it —
        # but not when they started a message while the hand-off was tried: then it waits.
        assert gate(app).has_focus
        inp = app.query_one(ChatInput)
        inp.text = "meanwhile"
        previous_gate = gate(app)
        with patch("shutil.which", return_value=None):
            await pilot.press("down", "down", "enter")
            assert await wait_until(pilot, lambda: gate(app) is not None and gate(app) is not previous_gate)
        await pilot.pause(0.2)
        assert inp.has_focus and not gate(app).has_focus and inp.text == "meanwhile", \
            "the plan card took the keyboard from a message being written"
        inp.text = ""
        await pilot.press("enter")  # nothing to send: Enter goes to the card
        assert await wait_until(pilot, lambda: gate(app).has_focus)
        await pilot.press("escape")
        assert await wait_until(pilot, lambda: gate(app) is None)
        assert eng.planning.active and eng.planning.status == "planning"
        # Revising the on-disk file after review invalidates that approval.
        await app._handle_plan("/plan")
        assert await wait_until(pilot, lambda: gate(app) is not None)
        eng.plan_file.write_text("revised scope")
        await pilot.press("down", "enter")
        assert await wait_until(pilot, lambda: gate(app) is None)
        assert eng.planning.active and not app._plan_go

    # Bare /plan uses earlier conversation; the topic shortcut is the same mode.
    target["path"] = None
    for command in ("/plan", "/plan design the greeting"):
        app, eng = build()
        eng._append({"role": "user", "content": "add a greeting feature"})
        async with app.run_test(size=(95, 34)) as pilot:
            await pilot.pause(0.1)
            await app._handle_plan(command)
            assert await wait_until(pilot, lambda: app._turn_worker.is_finished)
            assert eng.planning.active and len(eng.history) > 2
            last_user = [m["content"] for m in eng.history if m["role"] == "user"][-1]
            assert ("existing conversation" in last_user if command == "/plan"
                    else "design the greeting" in last_user)

    # Retry on the reopened gate once Docker answers (even with no executable on
    # this host), and build-here after a failed handoff still submits the turn.
    target["submit"] = True
    for retry in (True, False):
        app, eng = build()
        captured = {}
        async with app.run_test(size=(95, 34)) as pilot:
            await pilot.pause(0.1)
            await app._handle_plan("/plan")
            target["path"] = str(eng.plan_file)
            plan_path = eng.plan_file
            app._docker_ready = AsyncMock(return_value=False)
            app._make_goal_backend = lambda obj, plan_file=None, plan_text=None: (
                captured.update(pf=plan_file), FakeGoalBackend())[1]
            inp = app.query_one(ChatInput)
            inp.focus()
            inp.text = "plan it"
            await pilot.press("enter")
            assert await wait_until(pilot, lambda: gate(app) is not None)
            previous_gate = gate(app)
            with patch("shutil.which", return_value=None):
                await come_to_card(pilot, app)
                await pilot.press("down", "down", "enter")
                assert await wait_until(pilot, lambda: gate(app) is not None and gate(app) is not previous_gate)
            assert not captured, "Docker unavailable: no goal backend is created"
            if retry:
                app._docker_ready = AsyncMock(return_value=True)
                with patch("shutil.which", return_value=None):
                    await pilot.press("down", "down", "enter")
                    assert await wait_until(pilot, lambda: any(isinstance(s, GoalScreen) for s in app.screen_stack)), \
                        "retry on the reopened gate should open the goal screen"
                assert captured.get("pf") == plan_path and eng.plan_file is None
                gs = next(s for s in app.screen_stack if isinstance(s, GoalScreen))
                assert await wait_until(pilot, lambda: gs._state == "confirm"), gs._state
                await pilot.press("y")
                assert await wait_until(pilot, lambda: gs._state == "done"), gs._state
                await pilot.press("enter")
                assert await wait_until(pilot, lambda: not any(isinstance(s, GoalScreen) for s in app.screen_stack))
            else:
                await pilot.press("down", "enter")
                assert await wait_until(pilot, lambda: app._plan_go is not None or any(
                    m.get("role") == "user" and "implement the plan" in str(m.get("content", ""))
                    for m in eng.history)), "build-here after a failed handoff must still submit the implement turn"
                assert eng.plan_file is None

    print("TUI PLAN SMOKE OK — /plan → draft → gate → approve/implement · g→goal · Docker failure reopens the gate · retry / build here · /plan off. amaze!")


asyncio.run(main())
