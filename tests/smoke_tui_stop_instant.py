"""A turn stopped at the wrong instant must not end rocky (TUI pilot, no API).

Two old bugs with one cause: a turn was stopped (Esc, a new message) while it
waited for a widget to be removed.
  - While the streaming reply was swapped for the rendered one: the turn's
    clean-up finalized the reply a second time, and rocky ended with
    "MountError: Unable to find relative location ... because it has no parent".
  - While an answered approval card was being removed: `await widget.remove()`
    joins Textual's own prune task, the App's message loop waits for that same
    task, and the cancel took both. rocky closed by itself, without a word.
Each needed a second key within a few steps of the event loop. Now:
  - a reply that is being finalized is claimed first, so finalizing again finds
    nothing to do, whenever the stop lands;
  - every wait for a removal goes through tui/removal.py `gone()`, behind a
    shield: the cancel stops the wait, not the removal, and not the app.
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.chdir(tempfile.mkdtemp(prefix="rockytuistop-"))
# Run by hand, this must not write a session into the developer's own store.
os.environ.setdefault("ROCKYCODE_HOME", tempfile.mkdtemp(prefix="rockytuistop-home-"))

from textual.widgets import Static  # noqa: E402

from rockycode.engine.loop import Engine  # noqa: E402
from rockycode.engine.tools import Tool  # noqa: E402
from rockycode.tui import cardkeys  # noqa: E402
from rockycode.tui.app import RockyCodeApp  # noqa: E402
from rockycode.tui.chatinput import ChatInput  # noqa: E402
from rockycode.tui.permission import InlineApproval  # noqa: E402
from rockycode.tui.removal import gone  # noqa: E402
from invariants import assert_history_api_valid, completed_chunks  # noqa: E402

cardkeys.ENTER_GUARD = 0  # the half second is tested in smoke_tui_card_keys.py; here Enter answers a card at once
SIZE = (100, 34)
TUI = _HERE.parent / "rockycode" / "tui"


class FakeUsage:
    def model_dump(self):
        return {"prompt_tokens": 30, "completion_tokens": 6}


def chunk(content=None, tool_calls=None, usage=None):
    delta = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(usage=usage, choices=[types.SimpleNamespace(delta=delta)])


def build(permission="yolo"):
    """An app whose model asks for one bash call and then says done."""
    ran: list[str] = []
    calls = {"n": 0}

    class Completions:
        async def create(self, **kwargs):
            calls["n"] += 1
            first = calls["n"] == 1

            async def reply():
                if first:
                    call = types.SimpleNamespace(index=0, id="c1", function=types.SimpleNamespace(
                        name="bash", arguments=json.dumps({"command": "pytest -q"})))
                    parts = [chunk(content="Running it. "), chunk(tool_calls=[call]), chunk(usage=FakeUsage())]
                else:
                    parts = [chunk(content="done."), chunk(usage=FakeUsage())]
                for part in completed_chunks(parts):
                    yield part
            return reply()

    async def bash(command):
        ran.append(command)
        return "ok"

    schema = {"type": "function", "function": {"name": "bash", "parameters": {"type": "object", "properties": {}}}}
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=Completions()))
    engine = Engine(model="fake", client=client, workdir=Path.cwd(),
                    registry={"bash": Tool(name="bash", schema=schema, fn=bash, risk="risky")})
    app = RockyCodeApp(engine, permission=permission, evolve="off")
    app._titled = True
    return app, engine, ran


async def until(pilot, cond, seconds=4.0):
    for _ in range(int(seconds / 0.025)):
        if cond():
            return True
        await pilot.pause(0.025)
    return bool(cond())


async def steps(count):
    for _ in range(count):
        await asyncio.sleep(0)


async def main():
    # ── no wait for a removal outside gone(): a new card must not bring the bug back ─
    bare = [f"{path.name}:{number}" for path in sorted(TUI.glob("*.py")) if path.name != "removal.py"
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if re.search(r"\bawait\s+[\w.()\[\]\"'#-]+\.remove\(\)", line)]
    assert not bare, f"`await x.remove()` joins Textual's prune task; use `await gone(x)` (tui/removal.py): {bare}"

    # ── a wait for a removal that is cancelled: the wait stops, the removal and the app go on ─
    app, engine, ran = build()
    async with app.run_test(size=SIZE) as pilot:
        for offset in range(0, 8):
            line = Static(f"line {offset}")
            await app._add(line)
            waiting = asyncio.create_task(gone(line))
            await steps(offset)
            waiting.cancel()
            try:
                await waiting
            except asyncio.CancelledError:
                pass
            await pilot.pause(0.05)
            assert app.is_running, f"cancelling a wait for a removal ended the app (cancel {offset} steps in)"
            if offset:  # at 0 the wait was cancelled before it began: nothing was asked to go
                assert await until(pilot, lambda: line.parent is None, 1.0), "the removal itself must still happen"

    # ── a turn stopped while its reply is swapped for the rendered one: finalized once ─
    app, engine, ran = build()
    async with app.run_test(size=SIZE) as pilot:
        for offset in range(0, 60):  # the swap takes about twenty steps; the old failure was three of them
            line = Static("Here is the answer.", classes="reply-stream")
            await app._add(line)
            app._reply_widget, app._reply_buf, app._reply_flushed = line, "Here is the answer, in a sentence. " * 4, 0
            turn = asyncio.create_task(app._finalize_reply())
            await steps(offset)
            turn.cancel()
            try:
                await turn
            except asyncio.CancelledError:
                pass
            await app._after_cancel()  # the stopped turn's clean-up: used to mount after a line that was going
            assert app.is_running and app._reply_widget is None, f"stopped {offset} steps into the swap"
            await pilot.pause()

    # ── the real keys: an approval card, Enter, then Esc a few steps later ────
    for offset in range(0, 7):
        app, engine, ran = build(permission="ask")
        async with app.run_test(size=SIZE) as pilot:
            box = app.query_one(ChatInput)
            box.focus()
            box.text = "fix the failing test"
            await pilot.press("enter")
            assert await until(pilot, lambda: len(app.query(InlineApproval)) == 1), "the approval card never came"
            await pilot.press("enter")  # come to the card: it never takes the keyboard by itself
            assert await until(pilot, lambda: isinstance(app.focused, InlineApproval)), "Enter did not bring the keyboard to the card"
            app.simulate_key("enter")   # Run once: the card answers and starts to go
            await steps(offset)
            app.simulate_key("escape")  # stop the turn in that instant
            await pilot.pause(0.3)
            assert app.is_running, f"Enter, then Esc {offset} steps later: rocky closed by itself"
            assert await until(pilot, lambda: not app._delivery.running and not len(app.query(InlineApproval))), \
                "the turn must end, stopped or done, with the card gone"
            assert_history_api_valid(engine.history)

    print("TUI STOP INSTANT SMOKE OK — stop turn at wrong instant: wait stop, removal go on, rocky stay. amaze!")


asyncio.run(asyncio.wait_for(main(), 180))
