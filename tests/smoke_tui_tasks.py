"""Real Textual task handoff, busy refusal, native resume and two-way mail."""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

_HOME = tempfile.TemporaryDirectory(prefix="rocky-tasks-home-")
os.environ["ROCKYCODE_HOME"] = _HOME.name
from rockycode import session
from rockycode.engine.loop import Engine
from rockycode.engine.delivery import TurnMessage
from rockycode.tui.app import RockyCodeApp
from rockycode.tui.chatinput import ChatInput
from rockycode.tui.tasks import TaskSwitch


def build(root, info=None):
    engine = Engine(model="fake", client=NS(), registry={}, workdir=root, thinking=False)
    app = RockyCodeApp(engine, resume_session=info, evolve="off")
    engine.hooks.clear()
    return app


async def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        app = build(root)
        async with app.run_test(size=(100, 36)):
            assert "create_new_task" in app.engine.registry
            source = app._task_session.id
            assert not app._task_session.registered  # a plain chat is not a task until /task or a tool is used
            said = []
            app._task_session.say = lambda text: said.append(text) or asyncio.sleep(0)
            await app._handle_slash("/task open rk_nope")
            assert said[-1] == app._t("tasks.err_unknown_task") and "Unknown" not in said[-1]
            assert app._task_session.registered  # first /task use registered it
            await app._handle_slash("/task list")
            assert said[-2].startswith(app._t("tasks.current", id=source))
            app._task_session.say = type(app._task_session).say.__get__(app._task_session)
            app.engine._append({"role": "user", "content": "parent context"})
            app.engine._append({"role": "assistant", "content": "parent response"})
            parent_path = app.engine.trajectory.path
            child = json.loads(await app.engine.registry["create_new_task"].fn(prompt="child handoff"))
            target = child["task_id"]
            assert app._task_session.id == source  # a tool cannot hijack the foreground
            assert app._task_session.links.get(target)["draft"] == "child handoff"
            await app.engine.registry["send_task_message"].fn(target=target, message="question")
            app._delivery.paused = True
            app._delivery.pending.append(TurnMessage("queued"))
            await app._handle_slash(f"/task open {target}")
            assert app.return_value is None
            await app._delivery.clear()
            app.query_one(ChatInput).text = "parent unfinished draft"
            finalized = []
            original = app.engine.finalize_outcome
            app.engine.finalize_outcome = lambda: finalized.append(original())
            await app._handle_slash(f"/task open {target}")
            await app.workers.wait_for_complete()  # the switch rides the ordinary exit flow
            assert finalized, "leaving a task must record its outcome"
        switched = app.return_value
        assert isinstance(switched, TaskSwitch) and switched.permission == app._permission_mode
        assert session.load_history(switched.info.path) == []
        child_app = build(root, switched.info)
        async with child_app.run_test(size=(100, 36)):
            assert child_app._task_session.id == target
            assert child_app.query_one(ChatInput).text == "child handoff"
            assert not any(m.get("content") == "parent context" for m in child_app.engine.history)
            inbox = json.loads(await child_app.engine.registry["read_task_messages"].fn())["messages"]
            assert inbox[0]["sender"] == source
            await child_app.engine.registry["send_task_message"].fn(target=source, message="answer")
            child_app.query_one(ChatInput).text = "keep child draft"
        parent_app = build(root, session._read_info(parent_path))
        async with parent_app.run_test(size=(100, 36)):
            assert parent_app._task_session.id == source
            assert parent_app.query_one(ChatInput).text == "parent unfinished draft"
            assert any(m.get("content") == "parent context" for m in parent_app.engine.history)
            inbox = json.loads(await parent_app.engine.registry["read_task_messages"].fn())["messages"]
            assert inbox[0]["content"] == "answer"
            await parent_app._handle_slash(f'/task use {inbox[0]["id"]}')
            assert "parent unfinished draft" in parent_app.query_one(ChatInput).text
            assert "answer" in parent_app.query_one(ChatInput).text
            assert not parent_app._delivery.running
        stale_app = build(root, session._read_info(parent_path))
        async with stale_app.run_test(size=(100, 36)):
            assert stale_app._task_session.id != source
            assert stale_app._task_session.links.get(stale_app._task_session.id)["parent"] == source
            assert stale_app._task_session.links.get(source)["path"] == str(parent_app.engine.trajectory.path)
    print("TUI TASKS OK: native resume, isolated child, busy switch guard, drafts, A→B→A, "
          "lazy registration, keyed refusals, exit flow on switch")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        _HOME.cleanup()
