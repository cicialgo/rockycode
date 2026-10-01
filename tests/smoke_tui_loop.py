"""/loop in the TUI (headless Textual pilot, fake DeepSeek, no API, no Docker).

Paths driven through the real RockyCodeApp:
  A) /loop 5m … → receipt + chip + the one-time ceiling hint; /loop → a card;
     a QUIET tick folds: one streak line, history rolled back, trajectory kept;
     a second quiet tick updates the SAME line.
  B) no tick starts mid-turn; a submit mid-tick cancels the tick and the loop
     stays alive; history stays API-valid.
  C) NOTE stays rendered; DONE ends the loop with a line; the chip clears.
  D) ask mode: a tick that needs approval mounts NO prompt, pauses the loop;
     /loop allow → the inline approval; `y` → resumed; the retry runs.
  E) yolo: the same command runs unprompted in a tick; block-tier still refused.
  F) rocky starts a loop itself (loop_start) through the normal approval; the
     tools refuse inside a tick (loops don't breed).
  G) /loop pause · resume · stop by command.
  H) /permission yolo resumes every loop paused FOR AN APPROVAL (a manual
     /loop pause stays); the next ask-tier tick runs and does not pause;
     block-tier is still refused.
  I) shift+tab and the permission picker land on the same resume.
  J) a tick that already denied ask-tier, then yolo, does not finish paused;
     the following tick runs the command.
  K) careful still pauses, with a prompt that names yolo / allow / resume;
     switching to ask does not resume; denying /loop allow says yolo resumes.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import types
from collections import deque
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.environ["ROCKYCODE_HOME"] = tempfile.mkdtemp(prefix="rockyhome-")
os.chdir(tempfile.mkdtemp(prefix="rockytuiloop-"))

from textual.widgets import Static

from rockycode.engine.cron import LoopBook, build_loop_tools
from rockycode.engine.loop import Engine
from rockycode.engine.tools import Tool
from rockycode.tui.app import ChatInput, RockyCodeApp
from rockycode.tui.loopcard import LoopCard
from rockycode.tui.permission import InlineApproval, PermissionPicker
from invariants import assert_history_api_valid

WD = Path.cwd()


class U:
    def model_dump(self):
        return {"prompt_tokens": 30, "completion_tokens": 6}


def chunk(content=None, tool_calls=None, usage=None):
    if usage is not None and content is None and tool_calls is None:
        return types.SimpleNamespace(usage=usage, choices=[])
    d = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(usage=usage, choices=[types.SimpleNamespace(delta=d)])


def tc(i, id_, name, args):
    return types.SimpleNamespace(index=i, id=id_, function=types.SimpleNamespace(name=name, arguments=args))


async def stream(chunks):
    for c in chunks:
        yield c


class Script:
    """A queue of scripted model responses, one per API call. Empty → 'ok.'"""
    def __init__(self):
        self.q = deque()
        self.n = 0
        self.calls = 0

    def text(self, s):
        self.q.append([chunk(content=s), chunk(usage=U())])
        return self

    def tool(self, name, args):
        self.n += 1
        self.q.append([chunk(tool_calls=[tc(0, f"c{self.n}", name, json.dumps(args))]), chunk(usage=U())])
        return self

    def stall(self, event):
        self.q.append(("stall", event))
        return self

    async def create(self, **kwargs):
        self.calls += 1
        if not self.q:
            return stream([chunk(content="ok."), chunk(usage=U())])
        item = self.q.popleft()
        if isinstance(item, tuple):
            await item[1].wait()
            return stream([chunk(content="ok."), chunk(usage=U())])
        return stream(item)


_BASH = {"type": "function", "function": {"name": "bash", "parameters": {"type": "object", "properties": {}}}}


def build(permission="ask"):
    ran = []

    async def bash_fn(command):
        ran.append(command)
        return f"ran: {command}"

    script = Script()
    registry = {"bash": Tool(name="bash", schema=_BASH, fn=bash_fn, risk="risky")}
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=script))
    engine = Engine(model="fake", client=client, workdir=WD, registry=registry)
    # same wiring as cli.py: the book + tools exist before the app does
    engine.loop_book = LoopBook()
    engine.registry.update(build_loop_tools(engine.loop_book))
    app = RockyCodeApp(engine, permission=permission)
    return app, engine, script, ran


def txt(app):
    return " ".join(str(w.render()) for w in app.query(Static))


def has_approval(app):
    return len(app.query(InlineApproval)) > 0


async def wait_until(pilot, cond, timeout=6.0, step=0.05):
    for _ in range(int(timeout / step)):
        await pilot.pause(step)
        if cond():
            return True
    return bool(cond())


async def submit(pilot, app, text):
    inp = app.query_one(ChatInput)
    inp.focus()
    inp.text = text
    await pilot.press("enter")


def fire(app, loop):
    """Make the loop due and pump once (what the 1 Hz timer would do)."""
    loop.next_due = 0.0
    app._loop_pump()


async def tick_done(pilot, app, loop, n):
    return await wait_until(pilot, lambda: loop.ticks >= n and app._tick_loop is None
                            and loop.last_verdict not in ("", "cancelled") or loop.status in ("done", "stopped"))


async def case_a():
    app, engine, script, ran = build("yolo")   # yolo: the tick's bash runs → the quiet path
    async with app.run_test(size=(100, 34)) as pilot:
        await submit(pilot, app, "/loop 5m check results/")
        assert await wait_until(pilot, lambda: "loop #1" in txt(app)), txt(app)
        lp = app._loops.get(1)
        assert lp is not None and lp.interval_s == 300 and lp.prompt == "check results/"
        t = txt(app)
        assert "no budget" in t and "want a ceiling?" in t and "1 loop" in t, t
        # /loop → the card; esc leaves
        await submit(pilot, app, "/loop")
        assert await wait_until(pilot, lambda: len(app.query(LoopCard)) == 1)
        await pilot.press("escape")
        assert await wait_until(pilot, lambda: len(app.query(LoopCard)) == 0)
        # a QUIET tick: one bash call, then the marker
        n_hist = len(engine.history)
        script.tool("bash", {"command": "ls results/"}).text("nothing new.\nLOOP QUIET")
        fire(app, lp)
        assert await tick_done(pilot, app, lp, 1), (lp.ticks, lp.last_verdict)
        assert lp.last_verdict == "quiet" and lp.quiet_streak == 1
        assert "ls results/" in ran
        t = txt(app)
        assert "quiet ×1" in t, t
        assert "tick 1 ·" not in t, "the tick's header must be folded away"
        assert len(engine.history) == n_hist, "quiet tick must roll back out of live context"
        assert_history_api_valid(engine.history)
        recs = [json.loads(l) for l in Path(engine.trajectory.path).read_text(encoding="utf-8").splitlines()]
        assert any(r["kind"] == "message" and "[loop #1 · tick 1" in str(r["data"].get("content", "")) for r in recs), \
            "the trajectory keeps the folded tick"
        assert any(r["kind"] == "note" and "rollback" in r["data"] for r in recs)
        # a second quiet tick updates the same line
        script.text("still nothing.\nLOOP QUIET")
        fire(app, lp)
        assert await tick_done(pilot, app, lp, 2)
        t = txt(app)
        assert "quiet ×2" in t and t.count("loop #1 · quiet") == 1, t
        assert len(engine.history) == n_hist
    print("  A ok — receipt · card · quiet fold + rollback")


async def case_a_ask_bash():
    """In ask mode a tick's bash needs approval → no prompt, the loop pauses."""
    app, engine, script, ran = build("ask")
    async with app.run_test(size=(100, 34)) as pilot:
        await submit(pilot, app, "/loop 5m check")
        assert await wait_until(pilot, lambda: app._loops.get(1) is not None)
        lp = app._loops.get(1)
        script.tool("bash", {"command": "ls"}).text("couldn't run it.\nLOOP NOTE — needs approval")
        fire(app, lp)
        saw_prompt = False
        for _ in range(40):
            await pilot.pause(0.05)
            saw_prompt = saw_prompt or has_approval(app)
            if app._tick_loop is None and lp.ticks == 1 and lp.status == "paused":
                break
        assert not saw_prompt, "a tick must never mount an approval"
        assert lp.status == "paused" and lp.blocked_on["tool"] == "bash" and "ls" not in ran
        t = txt(app)
        assert "paused" in t and "/loop allow 1" in t and "/loop resume 1" in t, t
        assert "/permission yolo" in t and "resumes it and runs unattended" in t, t
        assert "retries without a grant" in t, t
        assert_history_api_valid(engine.history)
    print("  A' ok — ask-mode tick never prompts, pauses, names yolo / allow / resume")


async def case_b():
    app, engine, script, ran = build("yolo")
    async with app.run_test(size=(100, 34)) as pilot:
        await submit(pilot, app, "/loop 5m check")
        assert await wait_until(pilot, lambda: app._loops.get(1) is not None)
        lp = app._loops.get(1)
        # a user turn that stalls → no tick may start
        gate = asyncio.Event()
        script.stall(gate)
        await submit(pilot, app, "hello")
        assert await wait_until(pilot, lambda: app._turn_worker is not None and app._turn_worker.is_running)
        fire(app, lp)
        await pilot.pause(0.4)
        assert lp.ticks == 0 and app._tick_loop is None, "no tick while a turn runs"
        gate.set()
        assert await wait_until(pilot, lambda: not app._turn_worker.is_running)
        # now the tick starts — and stalls; a submit cancels it, the loop lives
        gate2 = asyncio.Event()
        script.stall(gate2)
        app._loop_pump()
        assert await wait_until(pilot, lambda: app._tick_loop is not None), "tick should start when idle"
        await submit(pilot, app, "again")
        assert await wait_until(pilot, lambda: app._tick_loop is None and lp.last_verdict == "cancelled")
        assert lp.status == "running" and lp.ticks == 1
        assert "tick 1 interrupted by your message" in txt(app), txt(app)
        gate2.set()
        assert await wait_until(pilot, lambda: not app._turn_worker.is_running)
        assert_history_api_valid(engine.history)
        # Esc mid-tick → the tick-flavoured cancel line
        gate3 = asyncio.Event()
        script.stall(gate3)
        fire(app, lp)
        assert await wait_until(pilot, lambda: app._tick_loop is not None)
        await pilot.press("escape")
        assert await wait_until(pilot, lambda: "tick 2 cancelled" in txt(app)), txt(app)
        assert lp.status == "running"
        gate3.set()
        assert_history_api_valid(engine.history)
    print("  B ok — idle-only · submit/esc cancel the tick, not the loop")


async def case_c():
    app, engine, script, ran = build("yolo")
    async with app.run_test(size=(100, 34)) as pilot:
        await submit(pilot, app, "/loop 5m run the tests")
        assert await wait_until(pilot, lambda: app._loops.get(1) is not None)
        lp = app._loops.get(1)
        n_hist = len(engine.history)
        script.tool("bash", {"command": "pytest -q"}).text("2 failures in test_x.\nLOOP NOTE — 2 failures")
        fire(app, lp)
        assert await tick_done(pilot, app, lp, 1)
        t = txt(app)
        assert lp.last_verdict == "note" and "2 failures in test_x" in t and "tick 1" in t and "· note" in t, t
        assert len(engine.history) > n_hist, "a NOTE tick stays in context"
        script.text("all green now.\nLOOP DONE — tests pass")
        fire(app, lp)
        assert await wait_until(pilot, lambda: lp.status == "done")
        assert await wait_until(pilot, lambda: "✓ loop #1 done" in txt(app) and "tests pass" in txt(app)), txt(app)
        assert app._loops.live() == []
        await pilot.pause(0.5)  # chip refresh
        assert "1 loop" not in txt(app).split("📁", 1)[-1][:200], "chip must clear"
        assert_history_api_valid(engine.history)
    print("  C ok — NOTE stays · DONE ends")


async def case_d():
    app, engine, script, ran = build("ask")
    async with app.run_test(size=(100, 34)) as pilot:
        await submit(pilot, app, "/loop 5m install and check")
        assert await wait_until(pilot, lambda: app._loops.get(1) is not None)
        lp = app._loops.get(1)
        script.tool("bash", {"command": "pip install httpx"}).text("blocked.\nLOOP NOTE — needs pip install")
        fire(app, lp)
        assert await wait_until(pilot, lambda: lp.status == "paused")
        assert not has_approval(app) and "pip install httpx" not in ran
        assert lp.blocked_on["detail"] == "pip install httpx"
        # decide it now
        await submit(pilot, app, "/loop allow 1")
        assert await wait_until(pilot, lambda: has_approval(app)), "allow must mount the inline approval"
        await pilot.press("y")  # once
        assert await wait_until(pilot, lambda: lp.status == "running" and not has_approval(app))
        assert "resumed" in txt(app)
        # the retry runs — no prompt
        script.tool("bash", {"command": "pip install httpx"}).text("installed.\nLOOP NOTE — installed")
        app._loop_pump()  # resume made it due now
        assert await tick_done(pilot, app, lp, 2)
        assert "pip install httpx" in ran and lp.status == "running" and not has_approval(app), (ran, lp.status)
        assert_history_api_valid(engine.history)
        # deny path keeps it paused
        script.tool("bash", {"command": "pip install other"}).text("LOOP NOTE — blocked again")
        fire(app, lp)
        assert await wait_until(pilot, lambda: lp.status == "paused" and lp.ticks == 3)
        await submit(pilot, app, "/loop allow")
        assert await wait_until(pilot, lambda: has_approval(app))
        await pilot.press("n")
        assert await wait_until(pilot, lambda: not has_approval(app))
        t = txt(app)
        assert lp.status == "paused" and "stays paused" in t, t
        assert "retries without it" in t and "/permission yolo" in t and "resumes it" in t, t
    print("  D ok — pause · /loop allow · once · deny")


async def case_e():
    app, engine, script, ran = build("yolo")
    async with app.run_test(size=(100, 34)) as pilot:
        await submit(pilot, app, "/loop 5m install")
        assert await wait_until(pilot, lambda: app._loops.get(1) is not None)
        lp = app._loops.get(1)
        script.tool("bash", {"command": "pip install httpx"}).text("done.\nLOOP NOTE — installed")
        fire(app, lp)
        assert await tick_done(pilot, app, lp, 1)
        assert "pip install httpx" in ran and lp.status == "running" and not has_approval(app)
        script.tool("bash", {"command": "sudo rm -rf /"}).text("refused.\nLOOP NOTE — refused")
        fire(app, lp)
        assert await tick_done(pilot, app, lp, 2)
        assert "sudo rm -rf /" not in ran and "blocked a dangerous command" in txt(app)
        assert lp.status == "running", "block-tier is refused, not a reason to pause"
        assert_history_api_valid(engine.history)
    print("  E ok — yolo unattended · block-tier refused")


async def case_f():
    app, engine, script, ran = build("ask")
    async with app.run_test(size=(100, 34)) as pilot:
        # rocky sets up the loop from a sentence → normal approval → receipt
        script.tool("loop_start", {"interval": "10m", "prompt": "check the bench"}).text("I'll check every 10 minutes.")
        await submit(pilot, app, "run the bench and check every 10 minutes if it finished")
        assert await wait_until(pilot, lambda: has_approval(app))
        assert "every 10m — check the bench · no budget" in txt(app), txt(app)
        await pilot.press("y")
        assert await wait_until(pilot, lambda: app._loops.get(1) is not None)
        lp = app._loops.get(1)
        assert lp.origin == "model" and lp.interval_s == 600
        assert await wait_until(pilot, lambda: "rocky set it up" in txt(app) and "loop #1" in txt(app))
        tool_msgs = [m for m in engine.history if m.get("role") == "tool"]
        assert tool_msgs and "loop #1 started" in tool_msgs[-1]["content"]
        # inside a tick the tools refuse — no new loop, no pause
        script.tool("loop_start", {"interval": "5m", "prompt": "child"}).text("LOOP QUIET")
        fire(app, lp)
        assert await tick_done(pilot, app, lp, 1)
        assert len(app._loops.all()) == 1 and lp.status == "running", (len(app._loops.all()), lp.status)
        recs = [json.loads(l) for l in Path(engine.trajectory.path).read_text(encoding="utf-8").splitlines()]
        assert any(r["kind"] == "message" and "inside a tick" in str(r["data"].get("content", "")) for r in recs)
        # rocky stops it on request (loop_stop is safe-tier — no prompt)
        script.tool("loop_stop", {"id": 1}).text("stopped.")
        await submit(pilot, app, "ok stop checking")
        assert await wait_until(pilot, lambda: lp.status == "stopped")
        assert not has_approval(app) and "stopped by rocky" in txt(app)
        assert_history_api_valid(engine.history)
    print("  F ok — loop_start via approval · no breeding · loop_stop")


async def case_g():
    app, engine, script, ran = build("yolo")
    async with app.run_test(size=(100, 34)) as pilot:
        await submit(pilot, app, "/cron 2m ping")   # the alias
        assert await wait_until(pilot, lambda: app._loops.get(1) is not None)
        lp = app._loops.get(1)
        await submit(pilot, app, "/loop pause")
        assert await wait_until(pilot, lambda: lp.status == "paused")
        fire(app, lp)
        await pilot.pause(0.3)
        assert lp.ticks == 0, "a paused loop never fires"
        await submit(pilot, app, "/loop resume")
        assert await wait_until(pilot, lambda: lp.status == "running")
        await submit(pilot, app, "/loop stop")
        assert await wait_until(pilot, lambda: lp.status == "stopped")
        assert "✓ loop #1 stopped" in txt(app)
        await submit(pilot, app, "/loop stop")
        assert await wait_until(pilot, lambda: "no loops running" in txt(app))
        await submit(pilot, app, "/loop 5s x")
        assert await wait_until(pilot, lambda: "shortest interval" in txt(app))
    print("  G ok — pause · resume · stop · alias · floor")


def _due_or_going(app, lp, ticks_before, now):
    """Resumed: still due, already ticking, or a tick already landed and left it running."""
    return lp.status == "running" and lp.blocked_on is None and (
        lp.ticks > ticks_before or app._tick_loop is lp or lp.next_due <= now + 1)


async def case_h():
    """/permission yolo resumes permission-paused loops; a manual /loop pause
    stays paused. The next ask-tier tick runs; block-tier is refused and does
    not pause."""
    app, engine, script, ran = build("ask")
    async with app.run_test(size=(110, 40)) as pilot:
        await submit(pilot, app, "/loop 5m check one")
        assert await wait_until(pilot, lambda: app._loops.get(1) is not None)
        a = app._loops.get(1)
        script.tool("bash", {"command": "pip install httpx"}).text("needs approval\nLOOP NOTE — blocked")
        fire(app, a)
        assert await wait_until(pilot, lambda: a.status == "paused" and a.blocked_on)
        assert "pip install httpx" not in ran

        await submit(pilot, app, "/loop 5m check two")
        assert await wait_until(pilot, lambda: app._loops.get(2) is not None)
        b = app._loops.get(2)
        await submit(pilot, app, "/loop pause 2")
        assert await wait_until(pilot, lambda: b.status == "paused" and b.blocked_on is None)

        ticks_a, ticks_b = a.ticks, b.ticks
        script.tool("bash", {"command": "pip install httpx"}).text("installed.\nLOOP NOTE — ran")
        # (no second scripted turn: b stays paused, so only a ticks)
        await submit(pilot, app, "/permission yolo")
        assert await wait_until(pilot, lambda: app._permission_mode == "yolo"
                                 and "resumed 1 loop paused for approval" in txt(app)), txt(app)
        now = time.time()
        assert _due_or_going(app, a, ticks_a, now), (a.status, a.ticks, a.next_due, now)
        assert b.status == "paused" and b.blocked_on is None, "a manual /loop pause is not yolo's to undo"

        app._loop_pump()
        assert await wait_until(
            pilot,
            lambda: "pip install httpx" in ran and a.ticks > ticks_a
            and app._tick_loop is None,
            timeout=8,
        ), (ran, a.ticks, b.ticks, a.status, b.status, txt(app))
        assert a.status == "running"
        assert b.status == "paused" and b.ticks == ticks_b, "the parked loop never ticked"
        assert not has_approval(app)
        assert_history_api_valid(engine.history)

        script.tool("bash", {"command": "sudo rm -rf /"}).text("refused.\nLOOP NOTE — refused")
        fire(app, a)
        assert await tick_done(pilot, app, a, a.ticks + 1)
        assert "sudo rm -rf /" not in ran and "blocked a dangerous command" in txt(app)
        assert a.status == "running", "block-tier under yolo must not re-pause"
    print("  H ok — /permission yolo resumes approval-paused loops only; ask-tier runs; block stays refused")


async def case_i():
    """shift+tab and the chip picker both go through the same resume."""
    app, engine, script, ran = build("ask")
    async with app.run_test(size=(110, 40)) as pilot:
        await submit(pilot, app, "/loop 5m watch")
        assert await wait_until(pilot, lambda: app._loops.get(1) is not None)
        lp = app._loops.get(1)
        script.tool("bash", {"command": "pip install httpx"}).text("LOOP NOTE — blocked")
        fire(app, lp)
        assert await wait_until(pilot, lambda: lp.status == "paused")

        script.tool("bash", {"command": "pip install httpx"}).text("installed.\nLOOP NOTE — ran")
        app.query_one(ChatInput).focus()
        await pilot.press("shift+tab")
        assert await wait_until(pilot, lambda: app._permission_mode == "yolo"
                                 and "resumed 1 loop paused for approval" in txt(app)), txt(app)
        assert lp.status == "running" and lp.blocked_on is None
        app._loop_pump()
        assert await wait_until(
            pilot,
            lambda: "pip install httpx" in ran and lp.ticks >= 2 and lp.status == "running"
            and app._tick_loop is None,
            timeout=8,
        ), (ran, lp.ticks, lp.status, txt(app))
        assert not has_approval(app)

    app, engine, script, ran = build("ask")
    async with app.run_test(size=(110, 40)) as pilot:
        await submit(pilot, app, "/loop 5m watch")
        assert await wait_until(pilot, lambda: app._loops.get(1) is not None)
        lp = app._loops.get(1)
        script.tool("bash", {"command": "pip install httpx"}).text("LOOP NOTE — blocked")
        fire(app, lp)
        assert await wait_until(pilot, lambda: lp.status == "paused")
        await pilot.click("#permchip")
        assert await wait_until(pilot, lambda: len(app.query(PermissionPicker)) == 1)
        script.tool("bash", {"command": "pip install httpx"}).text("installed.\nLOOP NOTE — ran")
        await pilot.click("#perm-mode-2")  # yolo row
        assert await wait_until(pilot, lambda: app._permission_mode == "yolo"
                                 and "resumed 1 loop paused for approval" in txt(app)
                                 and not app.query(PermissionPicker)), txt(app)
        assert lp.status == "running"
        app._loop_pump()
        assert await wait_until(
            pilot,
            lambda: "pip install httpx" in ran and lp.status == "running" and lp.ticks >= 2
            and app._tick_loop is None,
            timeout=8,
        ), (ran, lp.status, lp.ticks)
        assert not has_approval(app)
    print("  I ok — shift+tab and the picker resume on yolo")


async def case_j():
    """Ask-tier denied mid-tick, then yolo: the tick must not finish paused,
    and the next tick runs the command."""
    app, engine, script, ran = build("ask")
    async with app.run_test(size=(110, 40)) as pilot:
        await submit(pilot, app, "/loop 5m install")
        assert await wait_until(pilot, lambda: app._loops.get(1) is not None)
        lp = app._loops.get(1)
        gate = asyncio.Event()
        script.tool("bash", {"command": "pip install httpx"}).stall(gate)
        fire(app, lp)
        assert await wait_until(pilot, lambda: app._tick_blocked is not None), "denial should be recorded mid-tick"
        assert lp.status == "running", "pause happens when the tick settles, not at the denial"
        app.query_one(ChatInput).focus()
        await pilot.press("shift+tab")
        assert await wait_until(pilot, lambda: app._permission_mode == "yolo"), txt(app)
        gate.set()
        assert await wait_until(pilot, lambda: app._tick_loop is None and lp.ticks >= 1)
        assert lp.status == "running", "yolo must not pause a tick that already hit ask-tier"
        assert "loop #1 paused" not in txt(app), txt(app)
        assert "pip install httpx" not in ran, "the in-flight denial already returned"

        script.tool("bash", {"command": "pip install httpx"}).text("installed.\nLOOP NOTE — ran")
        fire(app, lp)
        assert await tick_done(pilot, app, lp, 2)
        assert "pip install httpx" in ran and lp.status == "running" and not has_approval(app)
        assert_history_api_valid(engine.history)
    print("  J ok — mid-tick yolo does not stick the loop; the next ask-tier tick runs")


async def case_k():
    """Careful pauses with the recovery prompt. A non-yolo switch leaves it paused."""
    app, engine, script, ran = build("careful")
    async with app.run_test(size=(110, 40)) as pilot:
        await submit(pilot, app, "/loop 5m install")
        assert await wait_until(pilot, lambda: app._loops.get(1) is not None)
        lp = app._loops.get(1)
        started = txt(app)
        assert "/permission yolo" in started and "resumes it" in started, started
        assert "running under careful" in started, started
        script.tool("bash", {"command": "pip install httpx"}).text("LOOP NOTE — blocked")
        fire(app, lp)
        assert await wait_until(pilot, lambda: lp.status == "paused")
        assert not has_approval(app) and "pip install httpx" not in ran
        t = txt(app)
        assert "resumes it and runs unattended" in t, t
        assert "/loop allow 1" in t and "/loop resume 1" in t and "retries without a grant" in t, t

        await submit(pilot, app, "/permission ask")
        assert await wait_until(pilot, lambda: app._permission_mode == "ask")
        assert lp.status == "paused", "only yolo auto-resumes"
        assert "▶ resumed" not in txt(app), txt(app)

        await submit(pilot, app, "/loop allow 1")
        assert await wait_until(pilot, lambda: has_approval(app))
        await pilot.press("n")
        assert await wait_until(pilot, lambda: not has_approval(app))
        t = txt(app)
        assert lp.status == "paused" and "stays paused" in t, t
        assert "retries without it" in t and "/permission yolo" in t, t
    print("  K ok — careful pauses with a prompt; ask does not resume; deny names yolo")


async def main():
    await case_a()
    await case_a_ask_bash()
    await case_b()
    await case_c()
    await case_d()
    await case_e()
    await case_f()
    await case_g()
    await case_h()
    await case_i()
    await case_j()
    await case_k()
    print("TUI LOOP SMOKE OK — receipt · idle-only ticks · fold · never-prompt + allow · yolo resume · tools · verbs. amaze!")


asyncio.run(main())
