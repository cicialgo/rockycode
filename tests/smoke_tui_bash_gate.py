"""Command-aware bash gating in chat (headless Textual pilot, no API).

The bug this guards (found in a real /tmp/rocky-goal-test session): the model ran
`brew install` on the host with no prompt, because bash had been approved "for the
session" earlier on a benign command — and the per-TOOL session grant blanket-
covered ALL later shell. The permission layer now judges each command:

  - "allow session" on a benign command does NOT cover a later install/network/
    privileged command — that re-prompts every time (session_grantable=False)
  - a block-tier command (sudo rm -rf /) is refused outright, in every mode, with
    no way to approve it — the tool never runs
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
os.chdir(tempfile.mkdtemp(prefix="rockybashgate-"))

from textual.widgets import Static

from rockycode.engine.loop import Engine
from rockycode.engine.tools import Tool
from rockycode.tui.app import ChatInput, RockyCodeApp
from rockycode.tui.permission import InlineApproval
from invariants import assert_history_api_valid


class U:
    def model_dump(self):
        return {"prompt_tokens": 30, "completion_tokens": 6}


def chunk(content=None, tool_calls=None, usage=None):
    delta = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(usage=usage, choices=[types.SimpleNamespace(delta=delta)])


def tc(i, id_, name, args):
    return types.SimpleNamespace(index=i, id=id_, function=types.SimpleNamespace(name=name, arguments=args))


async def stream(chunks):
    for c in chunks:
        yield c


class FakeCompletions:
    """Calls bash once per command in `commands` (one per turn), then answers."""
    def __init__(self, commands):
        self.commands = commands
        self.calls = 0

    async def create(self, **kwargs):
        i = self.calls
        self.calls += 1
        if i < len(self.commands):
            return stream([
                chunk(tool_calls=[tc(0, f"c{i}", "bash", json.dumps({"command": self.commands[i]}))]),
                chunk(usage=U()),
            ])
        return stream([chunk(content="done."), chunk(usage=U())])


_SCHEMA = {"type": "function", "function": {"name": "bash", "parameters": {"type": "object", "properties": {}}}}


def build(commands, permission="ask"):
    ran = []

    async def bash_fn(command):
        ran.append(command)
        return f"ran: {command}"

    registry = {"bash": Tool(name="bash", schema=_SCHEMA, fn=bash_fn, risk="risky")}
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions(commands)))
    engine = Engine(model="fake", client=client, workdir=Path.cwd(), registry=registry)
    return RockyCodeApp(engine, permission=permission), engine, ran


def has_approval(app):
    return len(app.query(InlineApproval)) > 0


async def wait_until(pilot, cond, timeout=6.0, step=0.05):
    for _ in range(int(timeout / step)):
        await pilot.pause(step)
        if cond():
            return True
    return bool(cond())


async def submit(pilot, app, text="go"):
    inp = app.query_one(ChatInput)
    inp.focus()
    inp.text = text
    await pilot.press("enter")


def transcript_text(app):
    return " ".join(str(w.render()) for w in app.query(Static))


async def main():
    # === session grant does NOT cover a dangerous command ===
    app, engine, ran = build(["ls -la", "brew install python-tk"], permission="ask")
    async with app.run_test(size=(90, 30)) as pilot:
        await submit(pilot, app)
        # 1st approval: the benign `ls` — approve for the SESSION (press 'a')
        assert await wait_until(pilot, lambda: has_approval(app)), "benign bash should prompt first"
        await pilot.press("a")
        assert await wait_until(pilot, lambda: "ls -la" in ran), "ls should run after session-approve"
        # 2nd approval MUST appear for `brew install` despite the session grant
        assert await wait_until(pilot, lambda: has_approval(app)), \
            "brew install must RE-PROMPT — a session grant can't cover an install"
        assert "brew install python-tk" not in ran, "brew must not have run without approval"
        await pilot.press("y")   # now approve it
        assert await wait_until(pilot, lambda: "brew install python-tk" in ran), "approve → brew runs"
    assert_history_api_valid(engine.history)

    # === a block-tier command is refused outright — no prompt, never runs ===
    app, engine, ran = build(["sudo rm -rf /"], permission="ask")
    async with app.run_test(size=(90, 30)) as pilot:
        await submit(pilot, app)
        assert await wait_until(pilot, lambda: "blocked a dangerous command" in transcript_text(app)), \
            "block-tier command must be reported as blocked"
        assert not has_approval(app), "a blocked command must NOT offer an approval prompt"
        assert "sudo rm -rf /" not in ran, "a blocked command must never run"
        # the turn still finishes cleanly (model gets a denial, then answers)
        assert await wait_until(pilot, lambda: any(m["role"] == "tool" for m in engine.history)), engine.history
    assert_history_api_valid(engine.history)

    # === yolo still refuses block-tier, even with no prompts ===
    app, engine, ran = build(["sudo rm -rf /"], permission="yolo")
    async with app.run_test(size=(90, 30)) as pilot:
        await submit(pilot, app)
        assert await wait_until(pilot, lambda: "blocked a dangerous command" in transcript_text(app)), \
            "yolo must STILL block a block-tier command"
        assert "sudo rm -rf /" not in ran, "yolo must not run a block-tier command"
    assert_history_api_valid(engine.history)

    print("TUI BASH-GATE SMOKE OK — session grant excludes installs · block-tier refused (ask + yolo). amaze!")


asyncio.run(main())
