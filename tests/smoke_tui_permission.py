"""TUI permission flow (headless Textual pilot, no API).

Drives the real RockyCodeApp in `ask` mode through a turn where the model calls
bash (risky → must prompt), and exercises all outcomes of the INLINE approval
(mounted in the chat, not a modal — so history stays scrollable while it waits):
  y  → tool runs
  n  → tool blocked, model gets [denied]
  esc → whole turn cancelled, history still valid (tool_call backfilled)
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.chdir(tempfile.mkdtemp(prefix="rockytuiperm-"))

from textual.widgets import Static

from rockycode.engine.loop import Engine
from rockycode.engine.tools import Tool
from rockycode.tui.app import RockyCodeApp
from rockycode.tui.chatinput import ChatInput
from rockycode.tui.permission import InlineApproval
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
from invariants import assert_history_api_valid


class FakeUsage:
    def model_dump(self):
        return {"prompt_tokens": 30, "completion_tokens": 6}


def chunk(content=None, tool_calls=None, usage=None):
    delta = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(usage=usage, choices=[types.SimpleNamespace(delta=delta)])


def tc(index, id_, name, args):
    return types.SimpleNamespace(
        index=index, id=id_, function=types.SimpleNamespace(name=name, arguments=args))


async def stream_from(chunks):
    from invariants import completed_chunks
    for c in completed_chunks(chunks):
        yield c


class FakeCompletions:
    """Turn 1: call bash. Turn 2+: a final answer."""
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return stream_from([
                chunk(tool_calls=[tc(0, "call_b", "bash", '{"command":"echo hi"}')]),
                chunk(usage=FakeUsage()),
            ])
        return stream_from([chunk(content="done."), chunk(usage=FakeUsage())])


_SCHEMA = {"type": "function",
           "function": {"name": "bash", "parameters": {"type": "object", "properties": {}}}}


def build():
    ran = {"bash": False}

    async def bash_fn(command):
        ran["bash"] = True
        return f"ran: {command}"

    registry = {"bash": Tool(name="bash", schema=_SCHEMA, fn=bash_fn, risk="risky")}
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions()))
    engine = Engine(model="fake", client=client, workdir=Path.cwd(), registry=registry)
    return RockyCodeApp(engine, permission="ask"), engine, ran


def tool_results(engine):
    return [m["content"] for m in engine.history if m["role"] == "tool"]


async def wait_until(pilot, cond, timeout=5.0, step=0.05):
    """Poll until cond() is true or timeout. Robust to CPU load — the old fixed
    `pause(0.5); assert` raced when the whole suite ran this app under load."""
    for _ in range(int(timeout / step)):
        await pilot.pause(step)
        if cond():
            return True
    return bool(cond())


def has_approval(app):
    return len(app.query(InlineApproval)) > 0


def approval_focused(app):
    """The inline approval has focus (so y/n/enter/arrows reach it)."""
    return has_approval(app) and isinstance(app.focused, InlineApproval)


async def drive_to_modal(pilot, app):
    inp = app.query_one(ChatInput)
    inp.focus()
    inp.text = "please run a command"
    await pilot.press("enter")
    for _ in range(60):           # wait for the inline approval to mount, then go to it
        await pilot.pause(0.05)
        if has_approval(app):
            assert not approval_focused(app), "the card must not take the keyboard by itself"
            await come_to_card(pilot, app)
            return
    raise AssertionError(
        f"inline approval never appeared/focused (has={has_approval(app)}, focused={type(app.focused).__name__})")


async def main():
    # --- Enter on the default row: approve once → bash runs ---
    app, engine, ran = build()
    async with app.run_test(size=(90, 30)) as pilot:
        await drive_to_modal(pilot, app)
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: ran["bash"]), "approve must run the tool"
        assert await wait_until(pilot, lambda: any(r == "ran: echo hi" for r in tool_results(engine))), engine.history
    assert_history_api_valid(engine.history)

    # --- THE POINT OF INLINE: scroll history WHILE an approval is pending ---
    # The old modal stole the whole screen; the inline prompt must let the user
    # scroll up to re-read what led here (wheel + pageup + shift+↑↓) and still act.
    from textual.events import MouseScrollUp
    app, engine, ran = build()
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause(0.1)                     # let compose finish (#transcript exists)
        for i in range(80):                       # give the transcript real history
            await app._add(Static(f"history line {i}"))
        await drive_to_modal(pilot, app)          # inline approval up + focused
        tr = app.query_one("#transcript")
        tr.scroll_end(animate=False)
        await pilot.pause(0.1)
        bottom = tr.scroll_offset.y
        assert bottom > 0, "setup: transcript should have scrollable history"
        # keyboard: pageup scrolls the transcript even though the approval holds focus
        await pilot.press("pageup")
        assert await wait_until(pilot, lambda: tr.scroll_offset.y < bottom), \
            "pageup must scroll history while the inline approval is focused"
        # mouse wheel: app-level handler scrolls the transcript too
        y1 = tr.scroll_offset.y
        tr.post_message(MouseScrollUp(widget=tr, x=5, y=5, delta_x=0, delta_y=1, button=0,
                                      shift=False, meta=False, ctrl=False,
                                      screen_x=5, screen_y=5, style=None))
        assert await wait_until(pilot, lambda: tr.scroll_offset.y <= y1), "wheel must scroll while pending"
        # the approval survived the scrolling and still approves
        assert has_approval(app), "approval must survive scrolling the history"
        await pilot.press("pageup", "pageup", "pageup")  # well above the end: more than the card gives back
        await pilot.pause(0.2)
        assert tr.scroll_offset.y < tr.max_scroll_y - 12, (tr.scroll_offset.y, tr.max_scroll_y)
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: ran["bash"]), "approve after scrolling must still run the tool"
        # … and the user who scrolled up to read is not pulled back to the end when the card goes.
        assert await wait_until(pilot, lambda: not has_approval(app))
        await pilot.pause(0.4)
        assert tr.scroll_offset.y < tr.max_scroll_y - 2, \
            f"the transcript jumped to its end under a user who was reading ({tr.scroll_offset.y} of {tr.max_scroll_y})"
    assert_history_api_valid(engine.history)

    # --- Enter on the default (Run once) → approve (Codex/Claude-style) ---
    app, engine, ran = build()
    async with app.run_test(size=(90, 30)) as pilot:
        await drive_to_modal(pilot, app)
        await pilot.pause(0.1)          # let on_mount focus the option list
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: ran["bash"]), "Enter on the default (Run once) must approve"
    assert_history_api_valid(engine.history)

    # --- arrows: ↓↓ to Deny, Enter → tool blocked ---
    app, engine, ran = build()
    async with app.run_test(size=(90, 30)) as pilot:
        await drive_to_modal(pilot, app)
        await pilot.pause(0.1)
        await pilot.press("down")       # Run once → Allow session
        await pilot.press("down")       # Allow session → Deny
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: any(r.startswith("[denied]") for r in tool_results(engine))), engine.history
        assert ran["bash"] is False, "arrow to Deny + Enter must block the tool"
    assert_history_api_valid(engine.history)

    # --- the Deny row: deny → tool blocked, model gets [denied] ---
    app, engine, ran = build()
    async with app.run_test(size=(90, 30)) as pilot:
        await drive_to_modal(pilot, app)
        await pilot.press("down", "down", "enter")
        assert await wait_until(pilot, lambda: any(r.startswith("[denied]") for r in tool_results(engine))), engine.history
        assert ran["bash"] is False, "deny must NOT run the tool"
    assert_history_api_valid(engine.history)

    # --- esc: cancel the whole turn → history still valid (tool_call backfilled) ---
    app, engine, ran = build()
    async with app.run_test(size=(90, 30)) as pilot:
        await drive_to_modal(pilot, app)
        await pilot.press("escape")
        assert await wait_until(pilot, lambda: not has_approval(app)), "inline approval should be removed after esc"
        assert await wait_until(pilot, lambda: any("interrupted" in r for r in tool_results(engine))), engine.history
        assert ran["bash"] is False, "esc must NOT run the tool"
    # the assistant tool_calls message must still have a matching tool response
    assert_history_api_valid(engine.history)

    # --- esc BINDING mid-tool (no modal): interrupt a running turn, history valid ---
    # yolo → approver not wired → no prompt; the tool hangs, esc cancels the worker.
    started = {"v": False}

    async def hang_fn(command):
        started["v"] = True
        await asyncio.Event().wait()  # never completes — simulates a long tool
        return "never"

    reg = {"bash": Tool(name="bash", schema=_SCHEMA, fn=hang_fn, risk="risky")}
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions()))
    engine = Engine(model="fake", client=client, workdir=Path.cwd(), registry=reg)
    app = RockyCodeApp(engine, permission="yolo")
    async with app.run_test(size=(90, 30)) as pilot:
        inp = app.query_one(ChatInput)
        inp.focus()
        inp.text = "run the hanging command"
        await pilot.press("enter")
        for _ in range(40):
            await pilot.pause(0.05)
            if started["v"]:
                break
        assert started["v"], "tool never started"
        assert not has_approval(app), "yolo must not prompt"
        await pilot.press("escape")   # app-level cancel_turn binding → worker.cancel()
        # Prove the Esc BINDING cancelled it (not the run_test teardown): the
        # worker must already be stopped while we're still inside the context.
        assert await wait_until(pilot, lambda: app._turn_worker is not None and not app._turn_worker.is_running), \
            "esc binding did not cancel the running turn (key swallowed?)"
        # the persistent permission chip is always visible in the status bar
        assert "YOLO" in str(app.query_one("#permchip", Static).render()), "persistent YOLO chip missing"
    assert_history_api_valid(engine.history)
    assert any("interrupted" in r for r in tool_results(engine)), engine.history

    # --- persistent chip + loud warning when a project config weakened the mode ---
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions()))
    eng = Engine(model="fake", client=client, workdir=Path.cwd())
    app = RockyCodeApp(eng, permission="yolo", permission_weakened=True)
    async with app.run_test(size=(90, 30)) as pilot:
        assert await wait_until(pilot, lambda: any("lowered permission" in str(w.render()) for w in app.query(Static))), \
            "project-weakening warning missing"
        assert "YOLO" in str(app.query_one("#permchip", Static).render()), "YOLO chip missing from status bar"

    print("TUI PERMISSION SMOKE OK — inline approve / deny / scroll-while-pending / esc / esc-binding / chip+warn. amaze!")


asyncio.run(main())
