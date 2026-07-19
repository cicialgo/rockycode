"""Plan mode in the TUI (headless Textual pilot, no API).

Drives the real RockyCodeApp through: /plan → the model drafts the plan file
(a write the read-only gate ALLOWS) → turn-end pops the inline plan gate →
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

from textual.widgets import Static

from rockycode.engine.loop import Engine
from rockycode.engine.tools import build_registry
from rockycode.tui.app import ChatInput, RockyCodeApp
from rockycode.tui.plangate import InlinePlanGate

# what the fake model writes as the plan (target["path"] is set after /plan)
target = {"path": None, "content": "## Phase 1 — setup\n- create foo.py\n## Phase 2 — test\n- add a test"}


class U:
    def model_dump(self):
        return {"prompt_tokens": 30, "completion_tokens": 6}


def chunk(content=None, tool_calls=None, usage=None):
    d = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(usage=usage, choices=[types.SimpleNamespace(delta=d)])


def tc(i, id_, name, args):
    return types.SimpleNamespace(index=i, id=id_, function=types.SimpleNamespace(name=name, arguments=args))


async def stream(chunks):
    for c in chunks:
        yield c


class FakeCompletions:
    """Turn 1: draft the plan (write to the plan file). Then: a plain answer."""
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
        return stream([chunk(content="done."), chunk(usage=U())])


def build():
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions()))
    eng = Engine(model="fake", client=client, workdir=Path.cwd(), registry=build_registry(Path.cwd()))
    return RockyCodeApp(eng, permission="yolo"), eng


def gate(app):
    q = app.query(InlinePlanGate)
    return q.first() if len(q) else None


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
        await app._handle_plan("/plan hello")
        assert eng.plan_file is not None, "/plan must set a plan file"
        target["path"] = str(eng.plan_file)
        assert "📋 plan" in str(app.query_one("#cwd", Static).render()), "plan chip missing"

        # 2) a turn where the model drafts the plan → gate pops at turn end
        inp = app.query_one(ChatInput)
        inp.focus()
        inp.text = "plan the hello feature"
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: gate(app) is not None), "plan gate should pop after the draft"
        assert eng.plan_file.read_text().strip(), "the plan file should have been written"

        # 3) approve → plan mode off + the implement turn is queued/submitted
        await pilot.press("y")
        assert await wait_until(pilot, lambda: eng.plan_file is None), "approve must leave plan mode"
        assert await wait_until(pilot, lambda: any(
            "plan approved" in str(w.render()) for w in app.query(Static))), "approval line missing"
        # the synthetic implement turn carries the plan path into history
        assert await wait_until(pilot, lambda: any(
            m.get("role") == "user" and "implement the plan" in str(m.get("content", ""))
            for m in eng.history)), "implement turn should be submitted"

    # 4) /plan off leaves cleanly, keeping the file
    app, eng = build()
    async with app.run_test(size=(95, 34)) as pilot:
        await pilot.pause(0.1)
        await app._handle_plan("/plan x")
        pf = eng.plan_file
        assert pf is not None
        await app._handle_plan("/plan off")
        assert eng.plan_file is None, "/plan off must clear plan mode"
        assert pf.exists(), "/plan off keeps the file for reference"
        assert "📋 plan" not in str(app.query_one("#cwd", Static).render()), "chip should clear"

    # 5) g → hand the approved plan to goal (Docker mocked); goal gets the plan file
    from rockycode.engine.goal_session import GoalSummary, Permits
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
        await app._handle_plan("/plan handoff")
        target["path"] = str(eng.plan_file)
        plan_path = eng.plan_file
        app._docker_ready = _docker_yes
        app._make_goal_backend = lambda obj, plan_file=None: (
            captured.update(obj=obj, pf=plan_file), FakeGoalBackend())[1]
        inp = app.query_one(ChatInput)
        inp.focus()
        inp.text = "plan it"
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: gate(app) is not None), "gate should pop"
        await pilot.press("g")   # run as goal
        assert await wait_until(pilot, lambda: any(isinstance(s, GoalScreen) for s in app.screen_stack)), \
            "g should open the goal screen"
        # goal got the SAME approved plan file (skips its own planner)
        assert captured.get("pf") == plan_path, f"plan file not handed to goal: {captured}"
        assert eng.plan_file is None, "handoff leaves plan mode"
        gs = next(s for s in app.screen_stack if isinstance(s, GoalScreen))
        assert await wait_until(pilot, lambda: gs._state == "confirm"), gs._state
        await pilot.press("y")   # confirm the goal run
        assert await wait_until(pilot, lambda: gs._state == "done"), gs._state
        await pilot.press("enter")   # back to chat
        assert await wait_until(pilot, lambda: not any(isinstance(s, GoalScreen) for s in app.screen_stack)), \
            "should return to chat"

    print("TUI PLAN SMOKE OK — /plan → draft → gate → approve/implement · g→goal handoff · /plan off. amaze!")


asyncio.run(main())
