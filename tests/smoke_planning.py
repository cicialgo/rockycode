"""Interactive planning lifecycle and engine gate, without API calls or Docker."""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

os.environ["ROCKYCODE_HOME"] = tempfile.mkdtemp(prefix="planning-home-")

from rockycode.goal.session import LiveGoalBackend
from rockycode.engine.loop import Engine
from rockycode.server import SessionManager
from rockycode.engine.sandbox import build_sandbox_registry
from rockycode.session import load_history, load_planning_state


class Fake:
    def __init__(self, calls=()):
        self.calls = list(calls)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        call = self.calls.pop(0) if self.calls else None

        async def stream():
            tc = None if call is None else [NS(index=0, id=f"c{len(self.requests)}",
                function=NS(name=call[0], arguments=json.dumps(call[1])))]
            yield NS(usage=None, choices=[NS(finish_reason="tool_calls" if tc else "stop", delta=NS(reasoning_content=None,
                     content="Let's discuss the approach." if call is None else None, tool_calls=tc))])
        return stream()


def build(wd, fake=None):
    fake = fake or Fake()
    eng = Engine(model="fake", client=NS(chat=NS(completions=fake)), workdir=wd,
                 system_prompt="Custom instructions: implement fixes.", max_steps=0)
    eng.enable_planning()
    return eng


async def turn(eng, message):
    return [ev async for ev in eng.run_turn(message)]


def rejects(fn):
    try:
        fn()
    except ValueError:
        return
    raise AssertionError("operation should have been rejected")


async def main():
    with tempfile.TemporaryDirectory(prefix="planning-") as td:
        wd = Path(td).resolve()
        fake = Fake()
        eng = build(wd, fake)
        base = eng.history[0]["content"]
        schema = eng.registry["submit_plan"].schema
        assert "[error]" in await eng.registry["submit_plan"].fn()
        path = eng.planning.start()
        assert eng.planning.start() == path
        assert ".rockycode/plans/" in eng.history[0]["content"]
        assert "overrides instructions to implement" in eng.history[0]["content"]
        rejects(eng.planning.request_review)
        # Actual model tool loop: yolo/default approver cannot bypass the plan gate.
        fake.calls = [("write_file", {"path": "code.py", "content": "unapproved"}),
                      ("write_file", {"path": str(path), "content": "## Fix\n- keep scope small"}),
                      ("submit_plan", {})]
        await turn(eng, "implement it immediately")
        assert not (wd / "code.py").exists()
        assert path.read_text() == "## Fix\n- keep scope small"
        assert eng.planning.status == "review" and eng.plan_file == path
        assert any("[blocked]" in str(m.get("content")) for m in eng.history if m["role"] == "tool")

        # Resume restores the gate and the reviewed revision, not a cached system prompt.
        saved = load_planning_state(eng.trajectory.path)
        assert saved == eng.planning.snapshot()
        resumed = build(wd)
        resumed.resume(load_history(eng.trajectory.path), planning_state=saved)
        assert resumed.plan_file == path and resumed.planning.status == "review"
        assert resumed._plan_gate("write_file", '{"path":"code.py"}').action == "deny"
        resumed.set_mode("research", "Discuss evidence.")
        assert "Active planning contract" in resumed.history[0]["content"]
        resumed.set_base_system("Updated base")
        assert "Active planning contract" in resumed.history[0]["content"]
        resumed.clear_mode()
        assert "Active planning contract" in resumed.history[0]["content"]

        # Plain chat is discussion, including the word 'approve'; it never switches modes.
        await turn(resumed, "approve, but add tests")
        assert resumed.planning.status == "planning"
        reviewed = resumed.planning.request_review()  # unchanged draft can be resubmitted
        path.write_text("## New scope\n- different work")
        rejects(lambda: resumed.planning.approve(reviewed["revision"]))
        assert resumed.planning.active
        revision = resumed.planning.request_review()["revision"]
        exact = path.read_text()
        request = resumed.planning.approve(revision)
        path.write_text("replacement after approval")
        assert exact in request and "replacement after approval" not in request
        assert resumed.plan_file is None and resumed.history[0]["content"] == "Updated base"
        rejects(lambda: resumed.planning.approve(revision))  # approvals cannot replay
        another = build(wd)
        another.resume([], planning_state=load_planning_state(resumed.trajectory.path))
        assert not another.planning.active

        # Starting twice across sessions never shares an empty draft.
        eng.planning.leave()
        assert eng.history[0]["content"] == base
        assert eng.registry["submit_plan"].schema == schema
        one = eng.planning.start()
        two = another.planning.start()
        assert one != two
        eng.swap_registry({})
        assert "submit_plan" in eng.registry
        eng.swap_registry(build_sandbox_registry(None))  # policy test; no Docker execution
        container_path = "/workspace/" + str(one.relative_to(wd))
        assert eng._plan_gate("write_file", json.dumps({"path": container_path})).action == "allow"
        assert eng._plan_gate("write_file", json.dumps({"path": str(one)})).action == "deny"
        assert eng._plan_gate("write_file", '{"path":"/workspace/code.py"}').action == "deny"
        # A file replaced by a symlink must not become a writable code-file alias.
        code = wd / "code.py"
        code.write_text("original")
        one.unlink()
        one.symlink_to(code)
        rejects(eng.planning.read)
        assert eng._plan_gate("write_file", json.dumps({"path": str(one)})).action == "deny"
        rejects(lambda: eng.planning.attach(code))

        # A stale saved review resumes as discussion, still read-only.
        another.planning.path.write_text("## First\n- step")
        old = another.planning.request_review()
        another.planning.path.write_text("## Second\n- changed")
        restored = build(wd)
        restored.resume([], planning_state=old)
        assert restored.planning.status == "planning"
        rejects(lambda: restored.resume([], planning_state={"status": "planning", "path": "code.py"}))
        assert restored.planning.active, "invalid resume must not disable an existing gate"

        # Bounded planning turns never receive the implementation closing notice.
        from rockycode.engine.loop import budget_notice
        short = build(wd, Fake([("read_file", {"path": "code.py"})]))
        short.max_steps, short.finalize_steps = 1, 1
        short.planning.start()
        await turn(short, "investigate this")
        closing = budget_notice(short.finalize_steps, closing=True)
        assert not any(m.get("content") == closing for m in short.history)
        assert any("Planning turn budget reached" in str(m.get("content")) for m in short.history)

        # Goal handoff freezes the full text and preserves more than 12 phases.
        approved = "Scope: preserve prose constraints.\n" + "\n".join(
            f"## Phase {n}\n- step {n}" for n in range(15))
        backend = LiveGoalBackend("task", model="fake", reviewer_model="fake", budget=None,
                                  workdir=wd, plan_file=two, plan_text=approved)
        two.write_text("different file")
        phases, supplement = await backend.plan()
        assert len(phases) == 15 and supplement == approved

        # The implementing engine, not just the milestone reviewer, receives
        # prose constraints and any later Goal-gate revisions. No Docker starts.
        from unittest.mock import AsyncMock, patch
        captured = {}
        backend.ws = NS(path=wd)
        backend.budget = NS(start=lambda: None)
        backend._client = NS(chat=NS(completions=Fake()))
        backend.driver = NS(attach=lambda engine, **kw: captured.update(engine=engine))
        backend._summary = lambda status, reason, *counts: NS(status=status)

        class Runner:
            def __init__(self, objective, *args, **kwargs):
                captured["objective"] = objective

            async def run(self):
                return NS(status="done", reason="verified", milestones_done=1, milestones_total=1)

        sandbox = NS(stop=AsyncMock())
        with patch("rockycode.engine.sandbox.ChatSandbox.start", AsyncMock(return_value=sandbox)), \
                patch("rockycode.goal.runner.GoalRunner", Runner):
            result = await backend.run(["Revised final milestone"], NS(use_network=False, approved=set()),
                                       lambda event: None)
        assert result.status == "done"
        context = captured["engine"].history[-1]["content"]
        assert approved in context and "Revised final milestone" in context
        assert "different file" not in context
        sandbox.stop.assert_awaited_once()

        # RPC clients use the same lifecycle and require a matching human revision.
        mgr = SessionManager("fake", wd, client=NS(chat=NS(completions=Fake())))
        sess = mgr._new_session()
        sid = sess.session_id
        state, message = mgr.plan_action(sid, "start")
        assert state["status"] == "planning" and message is None
        sess.engine.planning.path.write_text("## RPC\n- test")
        state, _ = mgr.plan_action(sid, "review")
        rejects(lambda: mgr.plan_action(sid, "approve", revision="wrong"))
        sess._task = asyncio.current_task()
        rejects(lambda: mgr.plan_action(sid, "leave"))
        assert mgr.plan_action(sid, "status")[0]["status"] == "review"
        sess._task = None
        state, message = mgr.plan_action(sid, "approve", revision=state["revision"])
        assert state["status"] == "approved" and "## RPC" in message
        await mgr.shutdown()
    print("PLANNING SMOKE OK — read-only loop, review revisions, resume, goal snapshot, RPC")


asyncio.run(main())
