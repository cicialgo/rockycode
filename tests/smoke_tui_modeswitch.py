"""shift+tab approval-mode switching (headless pilot, no API).

The mode switch has three doors and this test walks all of them, because the
shortcut must never be the only one: shift+tab cycles, the status-bar chip is
clickable (and spells the key out), and bare /permission opens the same picker.
Also checks the two places the switch must stay quiet: while an approval prompt
is waiting, and the wrap that lands on `careful` instead of parking in yolo.
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.chdir(tempfile.mkdtemp(prefix="rockymodeswitch-"))

from textual.widgets import Static

from rockycode.engine.loop import Engine
from rockycode.engine.permission import CYCLE, next_mode
from rockycode.engine.tools import Tool
from rockycode.tui.app import ChatInput, RockyCodeApp
from rockycode.tui.permission import InlineApproval, PermissionPicker


class FakeUsage:
    def model_dump(self):
        return {"prompt_tokens": 20, "completion_tokens": 4}


def chunk(content=None, tool_calls=None, usage=None):
    d = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(usage=usage, choices=[types.SimpleNamespace(delta=d)])


def tc(i, id_, name, args):
    return types.SimpleNamespace(index=i, id=id_, function=types.SimpleNamespace(name=name, arguments=args))


async def stream_from(cs):
    for c in cs:
        yield c


class FakeCompletions:
    """Call bash at the start of every turn (last msg = user); else answer."""
    async def create(self, **kw):
        msgs = kw["messages"]
        if msgs[-1].get("role") == "user":
            return stream_from([
                chunk(tool_calls=[tc(0, f"call_{len(msgs)}", "bash", '{"command":"echo hi"}')]),
                chunk(usage=FakeUsage()),
            ])
        return stream_from([chunk(content="done."), chunk(usage=FakeUsage())])


_SCHEMA = {"type": "function",
           "function": {"name": "bash", "parameters": {"type": "object", "properties": {}}}}


def chip(app):
    return str(app.query_one("#permchip", Static).render())


def transcript(app):
    return " ".join(str(w.render()) for w in app.query(Static))


async def enter(pilot, app, text):
    inp = app.query_one(ChatInput)
    inp.focus()
    inp.text = text
    await pilot.press("enter")


async def wait_until(pilot, pred, tries=40):
    for _ in range(tries):
        await pilot.pause(0.05)
        if pred():
            return True
    return False


def check_policy():
    """The cycle is policy, not UI: one step always LOOSENS, and the wrap comes
    back to the tightest mode rather than leaving you in yolo."""
    assert CYCLE == ("careful", "ask", "yolo"), CYCLE
    assert next_mode("careful") == "ask"
    assert next_mode("ask") == "yolo"
    assert next_mode("yolo") == "careful", "wrap must land on the tightest mode"
    assert next_mode("nonsense") == "careful", "unknown mode must fail safe"
    assert next_mode("ask", -1) == "careful", "reverse step still walks the cycle"


async def main():
    check_policy()

    async def bash_fn(command):
        return f"ran {command}"

    reg = {"bash": Tool(name="bash", schema=_SCHEMA, fn=bash_fn, risk="risky")}
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions()))
    eng = Engine(model="fake", client=client, workdir=Path.cwd(), registry=reg)
    app = RockyCodeApp(eng, permission="ask")

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause(0.2)
        assert app._permission_mode == "ask"
        # The key is ON SCREEN — a shortcut nobody can see is a shortcut nobody uses.
        assert "ask" in chip(app) and "shift+tab" in chip(app), f"chip: {chip(app)!r}"

        # --- shift+tab cycles, from anywhere (the input holds focus) ----------
        app.query_one(ChatInput).focus()
        await pilot.press("shift+tab")
        assert await wait_until(pilot, lambda: app._permission_mode == "yolo"), \
            "shift+tab did not reach the app (swallowed by the input?)"
        assert "yolo" in chip(app), f"chip not repainted: {chip(app)!r}"
        assert "yolo runs on your machine" in transcript(app), \
            "switching to yolo must still print the host warning"

        await pilot.press("shift+tab")
        assert await wait_until(pilot, lambda: app._permission_mode == "careful"), \
            "cycle must wrap to careful, not stay in yolo"
        await pilot.press("shift+tab")
        assert await wait_until(pilot, lambda: app._permission_mode == "ask")

        # --- clicking the chip opens the picker; esc keeps the mode ----------
        await pilot.click("#permchip")
        assert await wait_until(pilot, lambda: len(app.query(PermissionPicker)) == 1), \
            "clicking the chip must open the picker"
        await pilot.press("escape")
        assert await wait_until(pilot, lambda: not app.query(PermissionPicker))
        assert app._permission_mode == "ask", "esc must keep the current mode"
        assert app.query_one(ChatInput).has_focus, "focus must go back to the input"

        # --- picker, keyboard: rows are tightest-first, so up = careful ------
        await pilot.click("#permchip")
        assert await wait_until(pilot, lambda: len(app.query(PermissionPicker)) == 1)
        await pilot.press("up")
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: app._permission_mode == "careful"), \
            "picker did not apply the highlighted mode"
        assert not app.query(PermissionPicker), "picker must close after a pick"

        # --- bare /permission opens the same picker; a row CLICK picks -------
        await enter(pilot, app, "/permission")
        assert await wait_until(pilot, lambda: len(app.query(PermissionPicker)) == 1), \
            "bare /permission must open the picker"
        await pilot.click("#perm-mode-1")   # the "ask" row
        assert await wait_until(pilot, lambda: app._permission_mode == "ask"), \
            "clicking a picker row must switch the mode"

        # --- inert while an approval is waiting ------------------------------
        await enter(pilot, app, "go")
        assert await wait_until(pilot, lambda: len(app.query(InlineApproval)) == 1), \
            "ask mode should have prompted for bash"
        await pilot.press("shift+tab")
        await pilot.pause(0.2)
        assert app._permission_mode == "ask", \
            "shift+tab must not move the goalposts while a prompt is waiting"
        assert len(app.query(InlineApproval)) == 1, "the approval must still be up"
        await pilot.press("n")
        await pilot.pause(0.3)

    print("TUI MODESWITCH SMOKE OK — shift+tab cycles, chip + /permission open the picker, "
          "wrap lands on careful, inert mid-approval. amaze!")


if __name__ == "__main__":
    asyncio.run(main())
