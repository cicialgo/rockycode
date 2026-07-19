"""TUI `!` shell passthrough smoke test (headless, no API calls)."""
import asyncio
import os
import tempfile
import types
from pathlib import Path

os.chdir(tempfile.mkdtemp(prefix="rockysmoke-"))

from textual.widgets import Static

from rockycode.engine.loop import Engine
from rockycode.tui.app import ChatInput, RockyCodeApp

client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=None))  # never called
engine = Engine(model="fake", client=client, workdir=Path.cwd())


async def main():
    app = RockyCodeApp(engine)
    async with app.run_test(size=(80, 24)) as pilot:
        inp = app.query_one(ChatInput)
        inp.focus()
        inp.text = "! echo 中文 amaze"
        await pilot.press("enter")
        # _run_shell runs in a worker (off the message pump) — wait, bounded, for
        # its output to actually land rather than racing a fixed sleep.
        for _ in range(100):
            await pilot.pause(0.02)
            if engine.history and "中文 amaze" in str(engine.history[-1].get("content", "")):
                break

        texts = [str(w.render()) for w in app.query(Static)]
        assert any("echo 中文 amaze" in t for t in texts), "command line not echoed"
        assert any("[exit 0]" in t and "中文 amaze" in t for t in texts), "output not shown"

        # output landed in rocky's context and the trajectory
        last = engine.history[-1]
        assert last["role"] == "user" and "中文 amaze" in last["content"], last
        assert "[user ran a shell command]" in last["content"]

        # bare "!" → usage hint, nothing appended to history
        n = len(engine.history)
        inp.text = "!"
        await pilot.press("enter")
        for _ in range(20):  # let the hint-only worker finish; it must not touch history
            await pilot.pause(0.02)
        assert len(engine.history) == n
        print("SMOKE OK — ! runs shell, rocky sees output. amaze!")


asyncio.run(main())
