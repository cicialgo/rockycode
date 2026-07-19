"""TUI runtime /permission toggle (headless pilot, no API).

Flips the approval mode mid-session and checks the gate follows it live: yolo
runs a risky tool with no modal; after `/permission ask` the same tool now
prompts; and tightening the mode clears the session allowlist.
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.chdir(tempfile.mkdtemp(prefix="rockytoggle-"))

from textual.widgets import Static

from rockycode.engine.loop import Engine
from rockycode.engine.tools import Tool
from rockycode.tui.app import ChatInput, RockyCodeApp
from rockycode.tui.permission import InlineApproval


def has_approval(app):
    return len(app.query(InlineApproval)) > 0


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


async def enter(pilot, app, text):
    inp = app.query_one(ChatInput)
    inp.focus()
    inp.text = text
    await pilot.press("enter")


async def main():
    ran = {"n": 0}

    async def bash_fn(command):
        ran["n"] += 1
        return f"ran {command}"

    reg = {"bash": Tool(name="bash", schema=_SCHEMA, fn=bash_fn, risk="risky")}
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions()))
    eng = Engine(model="fake", client=client, workdir=Path.cwd(), registry=reg)
    app = RockyCodeApp(eng, permission="yolo")

    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._permission_mode == "yolo"

        # Starting in yolo on the host → the nudge names the risk + points at /sandbox.
        startup = " ".join(str(w.render()) for w in app.query(Static))
        assert "yolo runs on your machine" in startup and "/sandbox on" in startup, \
            "yolo-on-host nudge missing at startup"

        # yolo: a risky tool runs with NO modal.
        await enter(pilot, app, "go")
        for _ in range(40):
            await pilot.pause(0.05)
            if ran["n"] >= 1:
                break
        await pilot.pause(0.2)
        assert ran["n"] == 1, "yolo should run bash once with no prompt"
        assert not has_approval(app), "yolo must not prompt"

        # Flip to ask at runtime via /permission.
        await enter(pilot, app, "/permission ask")
        await pilot.pause(0.2)
        assert app._permission_mode == "ask", "toggle did not apply"
        assert "ask" in str(app.query_one("#cwd", Static).render()), "chip not refreshed"

        # Now the SAME risky tool prompts.
        before = ran["n"]
        await enter(pilot, app, "go again")
        for _ in range(40):
            await pilot.pause(0.05)
            if has_approval(app):
                break
        assert has_approval(app), "ask mode must prompt after the toggle"
        await pilot.press("y")
        await pilot.pause(0.3)
        assert ran["n"] == before + 1, "approve(y) should run it"

        # Tightening clears the session allowlist; loosening leaves it.
        app._auto_approve.add("bash")
        app._set_permission_mode("careful")
        assert "bash" not in app._auto_approve, "tightening must clear the allowlist"
        app._auto_approve.add("bash")
        app._set_permission_mode("yolo")
        assert "bash" in app._auto_approve, "loosening must not clear the allowlist"

    print("TUI TOGGLE SMOKE OK — /permission flips the gate live; tighten clears allowlist. amaze!")


asyncio.run(main())
