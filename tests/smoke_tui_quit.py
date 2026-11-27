"""Quitting while rocky works leaves nothing behind (TUI pilot, no API).

ctrl+q or /exit in the middle of a turn used to tear the screen down under the
turn. The turn's clean-up then drew on a transcript that was gone, and the
delivery task died of it: "Task exception was never retrieved" at exit.

One rule now: once rocky is told to leave, nothing new starts and nothing draws.
  - the first ctrl+q stops what still runs, the way Esc stops it, and only then
    leaves: a reply half written, a tool running, an approval card waiting, and
    also a turn the message queue does not own (a /loop tick);
  - history stays valid for the API, and a command that waited for a yes never runs;
  - what is typed after that starts nothing: a message, a `! command`, a slash
    command. rocky says "rocky is leaving". Only /exit still means something;
  - a message that was still on its way when rocky left starts no turn;
  - rocky waits at most EXIT_STOP_TIMEOUT for the stop, also for a tool that
    refuses to stop, and a second ctrl+q or /exit leaves at once;
  - the final reflect (evolve on) sees a stopped turn, not half of one, and never
    starts after a second ctrl+q has left;
  - if the app closes under a running turn (the window is closed), that is not an
    error either;
  - stopping a turn, by Esc or by leaving, gives the event loop nothing to report
    (on Python 3.14 a stopped turn used to print "exception in shielded future").
"""
import asyncio
import gc
import json
import os
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.chdir(tempfile.mkdtemp(prefix="rockytuiquit-"))
# Run by hand, this must not write a session into the developer's own store.
os.environ.setdefault("ROCKYCODE_HOME", tempfile.mkdtemp(prefix="rockytuiquit-home-"))

from textual.widgets import Static  # noqa: E402

from rockycode.dream import reflect as reflect_mod  # noqa: E402
from rockycode.engine.cron import LoopBook, build_loop_tools  # noqa: E402
from rockycode.engine.loop import Engine  # noqa: E402
from rockycode.engine.tools import Tool  # noqa: E402
from rockycode.tui import evolve  # noqa: E402
from rockycode.tui.app import RockyCodeApp  # noqa: E402
from rockycode.tui.chatinput import ChatInput  # noqa: E402
from rockycode.tui.permission import InlineApproval  # noqa: E402
from invariants import assert_history_api_valid, completed_chunks  # noqa: E402

HALF, TOOL, CARD = "a reply half written", "a tool running", "an approval card waiting"
SIZE = (100, 34)


class FakeUsage:
    def model_dump(self):
        return {"prompt_tokens": 30, "completion_tokens": 6}


def chunk(content=None, tool_calls=None, usage=None):
    delta = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(usage=usage, choices=[types.SimpleNamespace(delta=delta)])


class World:
    """The fake model and the fake bash, and what they saw."""

    def __init__(self, state, slow_stop, deaf, reflect_secs):
        self.state, self.slow_stop, self.deaf, self.reflect_secs = state, slow_stop, deaf, reflect_secs
        self.hold = asyncio.Event()  # never set: the turn does not end by itself
        self.turns: list[list] = []     # the messages of every turn request
        self.reflects: list[list] = []  # the messages of every reflect request (it does not stream)
        self.ran: list[str] = []        # commands the fake bash started
        self.plain = 0                  # how many of the next turn requests get a plain answer and end
        self.app = None                 # set by build()
        self.left_at_reflect: list[bool] = []  # had rocky already left when each reflect request went out?

    async def create(self, **kwargs):
        messages = json.loads(json.dumps(kwargs.get("messages", []), default=str))
        if not kwargs.get("stream"):
            self.reflects.append(messages)
            self.left_at_reflect.append(bool(self.app._left))
            await asyncio.sleep(self.reflect_secs)
            answer = types.SimpleNamespace(content="not json")
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=answer)])
        self.turns.append(messages)
        return self._reply()

    async def _reply(self):
        if self.plain:
            self.plain -= 1
            for part in completed_chunks([chunk(content="hello."), chunk(usage=FakeUsage())]):
                yield part
            return
        yield chunk(content="I will look at the tests first. ")
        if self.state == HALF:
            await self.hold.wait()
        call = types.SimpleNamespace(index=0, id=f"c{len(self.turns)}", function=types.SimpleNamespace(
            name="bash", arguments=json.dumps({"command": "pytest -q"})))
        for part in completed_chunks([chunk(tool_calls=[call]), chunk(usage=FakeUsage())]):
            yield part

    async def bash(self, command):
        self.ran.append(command)
        try:
            await self.hold.wait()
        except asyncio.CancelledError:
            end = asyncio.get_running_loop().time() + self.slow_stop
            while (left := end - asyncio.get_running_loop().time()) > 0:
                try:
                    await asyncio.sleep(left)
                except asyncio.CancelledError:
                    if not self.deaf:
                        raise  # told a second time: an ordinary tool gives up its clean-up
            raise
        return "ok"


def build(state, *, slow_stop=0.0, deaf=False, loops=False, evolve_mode="off", reflect_secs=0.0):
    """An app whose first turn stands still in `state` until somebody stops it.
    slow_stop: seconds the tool needs to stop. deaf: it also ignores being told again."""
    world = World(state, slow_stop, deaf, reflect_secs)
    schema = {"type": "function", "function": {"name": "bash", "parameters": {"type": "object", "properties": {}}}}
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=world))
    engine = Engine(model="fake", client=client, workdir=Path.cwd(),
                    registry={"bash": Tool(name="bash", schema=schema, fn=world.bash, risk="risky")})
    if loops:
        engine.loop_book = LoopBook()
        engine.registry.update(build_loop_tools(engine.loop_book))
    app = RockyCodeApp(engine, permission="ask" if state == CARD else "yolo", evolve=evolve_mode)
    if evolve_mode == "auto":
        reflect_mod.attach(engine)
    app._titled = True
    world.app = app
    # What was true each time the app was told to leave: was a turn still running?
    app.left_with_turn = []
    leave = app.exit

    def exit_(*args, **kwargs):
        worker = app._turn_worker
        app.left_with_turn.append(bool(worker is not None and worker.is_running))
        return leave(*args, **kwargs)

    app.exit = exit_
    return app, engine, world


async def say(pilot, app, text):
    box = app.query_one(ChatInput)
    box.focus()
    box.text = text
    await pilot.press("enter")


async def until(pilot, cond, seconds=4.0):
    for _ in range(int(seconds / 0.025)):
        if cond():
            return True
        await pilot.pause(0.025)
    return bool(cond())


async def reach(pilot, app, world):
    """Wait until the running turn stands still in its state."""
    def there():
        worker = app._turn_worker
        if worker is None or not worker.is_running:
            return False
        return {HALF: bool(app._reply_buf), TOOL: bool(world.ran), CARD: bool(app.query(InlineApproval))}[world.state]
    assert await until(pilot, there), f"the turn never reached: {world.state}"


async def start_turn(pilot, app, world):
    await say(pilot, app, "fix the failing test")
    await reach(pilot, app, world)


async def gone(pilot, app, seconds=8.0):
    return await until(pilot, lambda: not app.is_running, seconds)


def shown(app, text):
    return any(text in str(widget.render()) for widget in app.query(Static))


def ended_well(app):
    return app.return_code in (None, 0)


async def main():
    loop = asyncio.get_running_loop()
    lost: list[str] = []  # what the event loop had to report by itself: a task nobody looked after
    loop.set_exception_handler(lambda _loop, context: lost.append(f"{context.get('message')}: {context.get('exception')!r}"))

    async def nothing_lost(what):
        for _ in range(2):
            gc.collect()  # a task that died unseen reports when it is collected
            await asyncio.sleep(0.05)
        assert not lost, f"{what}: {lost}"

    # ── ctrl+q with no turn running: nothing to stop ──────────────────────────
    app, engine, world = build(TOOL)
    async with app.run_test(size=SIZE) as pilot:
        started = loop.time()
        await pilot.press("ctrl+q")
        assert await gone(pilot, app), "ctrl+q with nothing running must leave"
        assert loop.time() - started < 1.0, "with nothing to stop, rocky must not wait"
    assert app.left_with_turn == [False] and ended_well(app)
    await nothing_lost("quit while idle")

    # ── Esc stops a turn the same way, and the event loop has nothing to report ─
    # (Python 3.14 reports a waiter that ends in an error behind a shield by itself.)
    app, engine, world = build(TOOL)
    async with app.run_test(size=SIZE) as pilot:
        await start_turn(pilot, app, world)
        await pilot.press("escape")
        assert await until(pilot, lambda: not app._delivery.running and not app._turn_worker.is_running), \
            "Esc must stop the turn"
        await nothing_lost("Esc with a tool running")
        await pilot.press("ctrl+q")
        assert await gone(pilot, app), "ctrl+q after Esc must leave"
    assert app.left_with_turn == [False] and ended_well(app)
    assert_history_api_valid(engine.history)
    await nothing_lost("quit after Esc")

    for state in (HALF, TOOL, CARD):
        # ── one ctrl+q: the turn is stopped first, then rocky leaves ──────────
        app, engine, world = build(state)
        async with app.run_test(size=SIZE) as pilot:
            await start_turn(pilot, app, world)
            await pilot.press("ctrl+q")
            assert await gone(pilot, app), f"ctrl+q must leave ({state})"
        assert app.left_with_turn == [False], f"rocky left while the turn still ran ({state}): {app.left_with_turn}"
        assert ended_well(app), f"rocky ended with an error ({state}): {app.return_code}"
        assert_history_api_valid(engine.history)
        if state == CARD:
            assert world.ran == [], "a command that waited for a yes must not run because rocky quit"
        await nothing_lost(f"ctrl+q with {state}")

        # ── the app closes under the turn (the window is closed): not an error ─
        app, engine, world = build(state)
        async with app.run_test(size=SIZE) as pilot:
            await start_turn(pilot, app, world)
        assert app.left_with_turn == [], "nobody asked to leave here: the app was closed from outside"
        if state == CARD:
            assert world.ran == []
        await nothing_lost(f"the app closed under {state}")

    # ── /exit typed in the middle of a turn is the same way out ──────────────
    app, engine, world = build(TOOL)
    async with app.run_test(size=SIZE) as pilot:
        await start_turn(pilot, app, world)
        await pilot.press(*"/exit", "enter")
        assert await gone(pilot, app), "/exit must leave while a tool runs"
    assert app.left_with_turn == [False], f"/exit left while the turn still ran: {app.left_with_turn}"
    assert_history_api_valid(engine.history)
    await nothing_lost("/exit with a tool running")

    # ── a turn the message queue does not own is stopped too: a /loop tick ───
    app, engine, world = build(TOOL, loops=True)
    async with app.run_test(size=SIZE) as pilot:
        await say(pilot, app, "/loop 5m check the results")
        assert await until(pilot, lambda: app._loops.get(1) is not None), "/loop must start a loop"
        app._loops.get(1).next_due = 0.0
        app._loop_pump()
        assert await until(pilot, lambda: app._tick_loop is not None and bool(world.ran)), "the tick never ran its tool"
        assert not app._delivery.running, "a tick is not the message queue's turn: that is the case tested here"
        await pilot.press("ctrl+q")
        assert await gone(pilot, app), "ctrl+q must leave while a tick runs"
    assert app.left_with_turn == [False], f"rocky left while the tick still ran: {app.left_with_turn}"
    assert_history_api_valid(engine.history)
    await nothing_lost("ctrl+q with a loop tick running")

    # ── a second ctrl+q leaves at once, while the first still stops a slow turn ─
    app, engine, world = build(TOOL, slow_stop=1.0)
    async with app.run_test(size=SIZE) as pilot:
        await start_turn(pilot, app, world)
        await pilot.press("ctrl+q")
        await pilot.pause(0.1)
        assert app.is_running and app.left_with_turn == [], "the first ctrl+q still waits for the slow tool"
        await pilot.press("ctrl+q")
        assert await gone(pilot, app, seconds=0.5), "the second ctrl+q must not wait for the slow tool"
    assert app.left_with_turn == [True], \
        f"only the second ctrl+q leaves, and the first must not ask again afterwards: {app.left_with_turn}"
    await nothing_lost("second ctrl+q")

    # ── typed after the first ctrl+q: nothing new starts, and /exit leaves at once ─
    app, engine, world = build(TOOL, slow_stop=2.5)
    marker = Path.cwd() / "after-quit.txt"
    async with app.run_test(size=SIZE) as pilot:
        await start_turn(pilot, app, world)
        await pilot.press("ctrl+q")
        await pilot.pause(0.1)
        await say(pilot, app, "and now the docs")
        assert await until(pilot, lambda: shown(app, "rocky is leaving"), 1.0), \
            "a message typed while rocky leaves must be answered: rocky is leaving, unsent"
        await say(pilot, app, f"! touch {marker.name}")
        await say(pilot, app, "/queue resume")
        await say(pilot, app, "/queue clear")
        await pilot.pause(0.15)
        assert app.is_running, "these start nothing and end nothing: the slow tool is still stopping"
        started = loop.time()
        await say(pilot, app, "/exit")
        assert await gone(pilot, app, seconds=1.2), "a second /exit must leave at once"
        assert loop.time() - started < 1.2
    assert ended_well(app), f"typing while rocky leaves ended it with an error: {app.return_code}"
    assert len(world.turns) == 1, f"the message typed after ctrl+q reached the model: {len(world.turns)} requests"
    assert not marker.exists(), "a `! command` typed after ctrl+q must not run"
    await nothing_lost("typing after the first ctrl+q")

    # ── a message was still on its way (it interrupts a slow turn) when rocky left ─
    app, engine, world = build(TOOL, slow_stop=1.0)
    async with app.run_test(size=SIZE) as pilot:
        await start_turn(pilot, app, world)
        await say(pilot, app, "no, look at the docs instead")  # interrupts: it waits for the slow tool to stop
        await pilot.pause(0.1)
        await pilot.press("ctrl+q")
        await pilot.pause(0.1)
        await pilot.press("ctrl+q")
        assert await gone(pilot, app, seconds=0.6), "the second ctrl+q leaves at once, a message on its way or not"
    assert ended_well(app)
    assert len(world.turns) == 1, f"a message on its way started a turn after rocky left: {len(world.turns)} requests"
    await nothing_lost("a message on its way when rocky left")

    # ── the wait has a hard limit: a tool slow to stop, then one that refuses ──
    limit, evolve.EXIT_STOP_TIMEOUT = evolve.EXIT_STOP_TIMEOUT, 0.2
    try:
        for deaf in (False, True):
            what = "a tool that refuses to stop" if deaf else "a tool slow to stop"
            app, engine, world = build(TOOL, slow_stop=2.0, deaf=deaf)
            async with app.run_test(size=SIZE) as pilot:
                await start_turn(pilot, app, world)
                started = loop.time()
                await pilot.press("ctrl+q")
                assert await gone(pilot, app, seconds=1.2), f"one ctrl+q must not wait for {what}"
            assert len(app.left_with_turn) == 1 and ended_well(app), f"rocky must leave once, and well: {app.left_with_turn}"
            assert_history_api_valid(engine.history)
            await asyncio.sleep(2.1 if deaf else 0)  # let the stubborn tool end, so its tasks are judged too
            await nothing_lost(f"ctrl+q with {what} (left after {loop.time() - started:.2f}s)")
    finally:
        evolve.EXIT_STOP_TIMEOUT = limit
    assert 1.0 <= evolve.EXIT_STOP_TIMEOUT <= 5.0, "the real limit: long enough to stop a turn, short enough to feel like quitting"

    # ── evolve on: the final reflect sees a stopped turn; typing meanwhile starts nothing ─
    app, engine, world = build(TOOL, evolve_mode="auto", reflect_secs=0.8)
    world.plain = 2  # two small turns first: a session worth a reflect
    async with app.run_test(size=SIZE) as pilot:
        for n in (1, 2):
            await say(pilot, app, "hello")
            assert await until(pilot, lambda: len(world.turns) == n and not app._delivery.running), "a plain turn must end"
        await start_turn(pilot, app, world)
        await pilot.press("ctrl+q")
        assert await until(pilot, lambda: bool(world.reflects), 2.0), "with evolve on, leaving runs the final reflect"
        await say(pilot, app, "one more thing")
        assert await until(pilot, lambda: shown(app, "rocky is leaving"), 0.6), "typed during the reflect: unsent"
        assert await gone(pilot, app), "rocky leaves after the reflect"
    assert app.left_with_turn == [False] and ended_well(app)
    assert len(world.reflects) == 1 and len(world.turns) == 3, \
        f"one reflect, and no new turn while leaving: {len(world.reflects)} reflects, {len(world.turns)} turns"
    assert_history_api_valid(world.reflects[0])  # the reflect was asked about a stopped turn, not half of one
    await nothing_lost("ctrl+q with evolve on")

    # ── evolve on, nothing running: a second ctrl+q in the very steps the first is leaving in ─
    for offset in range(0, 24):
        app, engine, world = build(TOOL, evolve_mode="auto", reflect_secs=0.2)
        world.plain = 2  # a session worth a reflect
        async with app.run_test(size=SIZE) as pilot:
            for n in (1, 2):
                await say(pilot, app, "hello")
                assert await until(pilot, lambda: len(world.turns) == n and not app._delivery.running), "a plain turn must end"
            app.simulate_key("ctrl+q")
            for _ in range(offset):
                await asyncio.sleep(0)
            app.simulate_key("ctrl+q")
            assert await gone(pilot, app), "two ctrl+q must leave"
        assert not any(world.left_at_reflect), \
            f"a reflect started after rocky had left (second ctrl+q {offset} steps after the first): {world.left_at_reflect}"
        await nothing_lost(f"second ctrl+q {offset} steps after the first, evolve on")

    print("TUI QUIT SMOKE OK — quit mid turn: stop first, then leave. nothing new start, nothing lost. amaze!")


asyncio.run(asyncio.wait_for(main(), 240))
