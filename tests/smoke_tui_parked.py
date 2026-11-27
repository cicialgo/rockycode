"""yolo never waits: a step only the user can allow is parked, not asked (TUI pilot, no API).

A long yolo run used to stand still at an approval card when the model reached
for rocky's keys or settings: the card waited with no time limit while nobody was
there. Now, in yolo:
  - the step is refused at once, the model is told to go on, no card appears;
  - the status bar counts it (⏸ chip) and the turn leaves one clickable line;
  - the card opens only when the user asks (the chip, the line, /parked), so a key
    typed for the next message can never land on it; its rows are clickable and
    Enter on the default decides nothing;
  - "run it now" tells rocky to pick the step up, and a pass for exactly that call
    exists only while that message's turn runs: a message that never runs allows
    nothing, and one pick-up turn never spends another's pass;
  - the third parked step in one turn stops the turn, in the middle of a batch too,
    with its usage counted and the queue held;
  - a parked card closes when another card needs the keyboard.
ask and careful keep the ordinary card, whose rows are now clickable too.
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
os.chdir(tempfile.mkdtemp(prefix="rockytuiparked-"))
# Run by hand, this must not write a session into the developer's own store.
os.environ.setdefault("ROCKYCODE_HOME", tempfile.mkdtemp(prefix="rockytuiparked-home-"))

from textual.widgets import Static  # noqa: E402

from rockycode.engine.loop import Engine  # noqa: E402
from rockycode.engine.tools import Tool  # noqa: E402
from rockycode.tui.app import RockyCodeApp  # noqa: E402
from rockycode.tui.approval import ParkedLine  # noqa: E402
from rockycode.tui.chatinput import ChatInput  # noqa: E402
from rockycode.tui.permission import InlineApproval  # noqa: E402
from rockycode.tui import cardkeys  # noqa: E402

cardkeys.ENTER_GUARD = 0  # the half second is tested in smoke_tui_card_keys.py; here Enter answers a card at once


async def come_to_card(pilot, app):
    """A card the user did not open never takes the keyboard: Enter in the empty message box goes to it."""
    await pilot.press("enter")
    for _ in range(80):
        await pilot.pause(0.025)
        if isinstance(app.focused, cardkeys.CardKeys):
            return
    raise AssertionError("Enter in the empty message box did not bring the keyboard to the card")
from invariants import assert_history_api_valid, completed_chunks  # noqa: E402

KEYS = "cat ~/.rockycode/config.toml"   # names a settings file: only the user can allow it (the bash here is a fake)
A, B, C = (f"cat ~/.rockycode/{name}" for name in (".env", "models.toml", "endpoints.toml"))


class FakeUsage:
    def model_dump(self):
        return {"prompt_tokens": 30, "completion_tokens": 6}


def chunk(content=None, tool_calls=None, usage=None):
    delta = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(usage=usage, choices=[types.SimpleNamespace(delta=delta)])


async def stream_from(chunks):
    for c in completed_chunks(chunks):
        yield c


class Script:
    """One model step per entry: "$ cmd" is one call, a list is one message with
    several calls, anything else is a sentence that ends the turn. `hold` makes
    the next answer wait for an event."""

    def __init__(self, steps):
        self.steps, self.calls, self.hold = list(steps), 0, None

    async def create(self, **kwargs):
        step = self.steps[self.calls] if self.calls < len(self.steps) else "done."
        self.calls += 1
        if self.hold is not None:
            hold, self.hold = self.hold, None
            await hold.wait()
        if isinstance(step, str) and not step.startswith("$ "):
            return stream_from([chunk(content=step), chunk(usage=FakeUsage())])
        commands = step if isinstance(step, list) else [step[2:]]
        calls = [types.SimpleNamespace(index=i, id=f"call_{self.calls}_{i}", function=types.SimpleNamespace(
            name="bash", arguments=json.dumps({"command": command}))) for i, command in enumerate(commands)]
        return stream_from([chunk(tool_calls=calls), chunk(usage=FakeUsage())])


_SCHEMA = {"type": "function", "function": {"name": "bash", "parameters": {"type": "object", "properties": {}}}}


def build(steps, mode="yolo"):
    ran, gate = [], asyncio.Event()

    async def bash_fn(command):
        if command == "sleep":
            await gate.wait()  # a long tool: the turn is running until the test lets it go
        ran.append(command)
        return f"ran: {command}"

    registry = {"bash": Tool(name="bash", schema=_SCHEMA, fn=bash_fn, risk="risky")}
    script = Script(steps)
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=script))
    engine = Engine(model="fake", client=client, workdir=Path.cwd(), registry=registry)
    app = RockyCodeApp(engine, permission=mode)
    app._titled = True  # no background title request: every model call here is a step of the script
    app.gate = gate
    return app, engine, ran, script


def tool_results(engine):
    return [m["content"] for m in engine.history if m["role"] == "tool"]


def said_by_user(engine):
    return [m["content"] for m in engine.history if m["role"] == "user"]


def shown(app):
    return "\n".join(str(w.render()) for w in app.query(Static))


async def wait_until(pilot, cond, timeout=6.0, step=0.05):
    for _ in range(int(timeout / step)):
        await pilot.pause(step)
        if cond():
            return True
    return bool(cond())


def idle(app):
    worker = app._turn_worker
    return not app._delivery.running and not (worker is not None and worker.is_running)


async def say(pilot, app, text):
    inp = app.query_one(ChatInput)
    inp.focus()
    inp.text = text
    await pilot.press("enter")


def card(app):
    cards = app.query(InlineApproval)
    return cards.first() if len(cards) else None


async def open_card(pilot, app, how="#parkchip"):
    await pilot.click(how)
    assert await wait_until(pilot, lambda: card(app) is not None and isinstance(app.focused, InlineApproval)), \
        f"clicking {how} must open the card"


async def main():
    # ── one parked step: no card, the run goes on, the user decides later ───────
    app, engine, ran, script = build([f"$ {KEYS}", "$ echo build", "built. one step waits for you.",
                                      f"$ {KEYS}", "read it."])
    async with app.run_test(size=(100, 34)) as pilot:
        await say(pilot, app, "build the project")
        assert await wait_until(pilot, lambda: script.calls >= 3 and idle(app)), script.calls
        assert card(app) is None, "yolo must not put a card in front of the run"
        assert ran == ["echo build"], f"the parked step did not run and the rest did: {ran}"
        first = tool_results(engine)[0]
        assert first.startswith("[parked]") and "another way" in first and "settings file" in first, first
        approver = app._approver
        assert len(approver.parked) == 1 and approver.parked[0].detail == KEYS and not approver.stop_turn
        chip = app.query_one("#parkchip", Static)
        assert chip.display and "1 waiting" in str(chip.render()), "the status bar counts it"
        assert "parked — waiting for you" in shown(app) and "Only the user can allow" not in shown(app), \
            "the tool line says parked in the user's words; the sentence for the model stays out of it"
        assert len(app.query(ParkedLine)) == 1 and "/parked" in shown(app), "the turn leaves one clickable line"
        assert isinstance(app.focused, ChatInput), "nothing took the keyboard away from the input"
        assert engine.stats.denials == 0, "a parked step is not the user saying no"

        # The user is back. Enter on the card's default decides nothing; Esc closes it the same way.
        await open_card(pilot, app)
        opened = card(app)
        assert [value for value, _ in opened._choices] == ["once", "deny", "later"]
        assert "waiting for you" in str(opened.border_title) and "names rocky's key file" in shown(app)
        assert app._idle_for_tick(), "no turn waits behind this card, so it holds no loop tick back"
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: card(app) is None)
        assert len(approver.parked) == 1 and not approver.once and script.calls == 3, "a stray Enter decides nothing"
        await open_card(pilot, app, how="ParkedLine")
        await pilot.press("escape")
        assert await wait_until(pilot, lambda: card(app) is None)
        assert len(approver.parked) == 1 and idle(app) and script.calls == 3, "Esc means later"
        await open_card(pilot, app)
        await pilot.click(ChatInput)  # the user goes back to typing: the card makes way, the step stays parked
        assert await wait_until(pilot, lambda: card(app) is None and isinstance(app.focused, ChatInput))
        assert len(approver.parked) == 1

        # Run it now, by clicking the row: rocky is told, and that exact call runs in that turn.
        await say(pilot, app, "/parked")
        assert await wait_until(pilot, lambda: card(app) is not None)
        await pilot.click("#perm-opt-0")
        assert await wait_until(pilot, lambda: ran == ["echo build", KEYS] and idle(app)), (ran, script.calls)
        assert not approver.parked and not approver.once and not app._pickups and not chip.display
        picked_up = said_by_user(engine)[-1]
        assert picked_up.startswith("I allow the step you parked") and json.dumps({"command": KEYS}) in picked_up
        assert "allowed once" in shown(app)
    assert_history_api_valid(engine.history)

    # ── the pass is for that exact call: a changed command parks again ─────────
    app, engine, ran, script = build([f"$ {KEYS}", "one step waits.", f"$ {KEYS} | head -1", "could not."])
    async with app.run_test(size=(100, 34)) as pilot:
        await say(pilot, app, "check the settings")
        assert await wait_until(pilot, lambda: script.calls >= 2 and idle(app))
        await open_card(pilot, app)
        await pilot.press("up", "up", "enter")
        assert await wait_until(pilot, lambda: script.calls >= 4 and idle(app)), script.calls
        approver = app._approver
        assert ran == [], "a different command is a different step"
        assert [p.detail for p in approver.parked] == [f"{KEYS} | head -1"]
        assert not approver.once, "the unused pass ended with the turn that was to use it"
    assert_history_api_valid(engine.history)

    # ── don't run it: off the list, rocky is told, no new turn ─────────────────
    app, engine, ran, script = build([f"$ {KEYS}", "one step waits."])
    async with app.run_test(size=(100, 34)) as pilot:
        await say(pilot, app, "check the settings")
        assert await wait_until(pilot, lambda: script.calls >= 2 and idle(app))
        await open_card(pilot, app)
        await pilot.press("up", "enter")
        assert await wait_until(pilot, lambda: card(app) is None and not app._approver.parked)
        await pilot.pause(0.3)
        assert script.calls == 2 and ran == [] and idle(app), "nothing runs and no turn starts"
        assert "decided not to run" in engine.history[-1]["content"] and KEYS in engine.history[-1]["content"]
        assert not app.query_one("#parkchip", Static).display and "not run" in shown(app)
        await say(pilot, app, "/parked")
        assert await wait_until(pilot, lambda: "nothing is waiting for you" in shown(app))
    assert_history_api_valid(engine.history)

    # ── the third parked step stops the turn: mid-batch, usage counted, the queue held ──
    app, engine, ran, script = build([[A, B, C, "echo four", "echo five"], "tidied.", f"$ {A}", "read the one you allowed."])
    async with app.run_test(size=(100, 34)) as pilot:
        app._delivery_mode = "queue"
        hold = script.hold = asyncio.Event()
        await say(pilot, app, "look around")
        assert await wait_until(pilot, lambda: script.calls == 1)
        await say(pilot, app, "then tidy up")  # typed while rocky works: it waits its turn
        assert await wait_until(pilot, lambda: len(app._delivery.pending) == 1)
        hold.set()                             # the model answers: one message, five calls
        approver = app._approver
        assert await wait_until(pilot, lambda: idle(app) and approver.stop_turn), (script.calls, approver.turn_parks)
        await pilot.pause(0.3)
        results = tool_results(engine)
        assert script.calls == 1 and ran == [], f"the model is not asked again and nothing ran: {script.calls} {ran}"
        assert [r.split("]")[0] for r in results] == ["[parked"] * 3 + ["[not run"] * 2, results
        assert "third such step" in results[2] and "rocky stopped this turn" in shown(app)
        assert len(approver.parked) == 3 and "3 waiting" in str(app.query_one("#parkchip", Static).render())
        assert app._ledger.totals()["prompt"] == 30, "a stopped turn still counts what it spent"
        assert app._delivery.paused and len(app._delivery.pending) == 1, \
            "a stop holds the queued message for the user, as Esc does"
        assert_history_api_valid(engine.history)
        # Two cards decided, the third left for later: one pick-up message names both.
        await open_card(pilot, app)
        await pilot.press("up", "up", "enter")
        assert await wait_until(pilot, lambda: card(app) is not None and len(approver.parked) == 2)
        await pilot.press("up", "enter")
        assert await wait_until(pilot, lambda: card(app) is not None and len(approver.parked) == 1)
        await pilot.press("escape")
        assert await wait_until(pilot, lambda: len(app._delivery.pending) == 2), "the pick-up waits behind the held queue"
        assert not approver.once and script.calls == 1, "nothing is allowed before its own turn runs"
        await say(pilot, app, "/queue resume")
        assert await wait_until(pilot, lambda: script.calls >= 4 and idle(app)), script.calls
        assert ran == [A], f"only the allowed step ran: {ran}"
        picked_up = said_by_user(engine)[-1]
        assert A in picked_up and "decided not to run" in picked_up and B in picked_up and C not in picked_up
        assert [p.detail for p in approver.parked] == [C] and not approver.once and not approver.stop_turn
    assert_history_api_valid(engine.history)

    # ── a card opened while rocky works: Esc is "later", "run" waits for the turn ─
    app, engine, ran, script = build([f"$ {KEYS}", "$ sleep", "long work done.", f"$ {KEYS}", "read it."])
    async with app.run_test(size=(100, 34)) as pilot:
        await say(pilot, app, "do the long thing")
        assert await wait_until(pilot, lambda: script.calls >= 2 and len(app._approver.parked) == 1)
        await open_card(pilot, app)
        await pilot.press("escape")
        assert await wait_until(pilot, lambda: card(app) is None)
        await pilot.pause(0.3)
        assert not idle(app) and script.calls == 2 and len(app._approver.parked) == 1, \
            "Esc on a parked card means later: it does not cancel the turn that is running"
        await open_card(pilot, app)
        await pilot.press("up", "up", "enter")
        await pilot.pause(0.3)
        assert script.calls == 2 and not idle(app), "allowing a step does not interrupt the running turn"
        assert not app._approver.once, "and the pass is not live in that turn: it belongs to the pick-up turn"
        app.gate.set()
        assert await wait_until(pilot, lambda: ran == ["sleep", KEYS] and idle(app)), (ran, script.calls)
    assert_history_api_valid(engine.history)

    # ── "don't run it" decided while rocky works: rocky is told when the turn ends ─
    app, engine, ran, script = build([f"$ {KEYS}", "$ sleep", "long work done."])
    async with app.run_test(size=(100, 34)) as pilot:
        await say(pilot, app, "do the long thing")
        assert await wait_until(pilot, lambda: script.calls >= 2 and len(app._approver.parked) == 1)
        await open_card(pilot, app)
        await pilot.press("up", "enter")
        assert await wait_until(pilot, lambda: card(app) is None and not app._approver.parked)
        assert not any("decided not to run" in text for text in said_by_user(engine)), \
            "not in the middle of the turn: a note there would split a tool call from its result"
        app.gate.set()
        assert await wait_until(pilot, lambda: idle(app) and script.calls == 3)
        assert "decided not to run" in engine.history[-1]["content"] and ran == ["sleep"]
    assert_history_api_valid(engine.history)

    # ── a pick-up that never runs allows nothing, now or in a stricter mode ────
    app, engine, ran, script = build([f"$ {KEYS}", "$ sleep", "done.", f"$ {KEYS}", "asked."])
    async with app.run_test(size=(100, 34)) as pilot:
        await say(pilot, app, "do the long thing")
        assert await wait_until(pilot, lambda: script.calls >= 2 and len(app._approver.parked) == 1)
        await open_card(pilot, app)
        await pilot.press("up", "up", "enter")
        assert await wait_until(pilot, lambda: len(app._delivery.pending) == 1)
        await say(pilot, app, "/queue clear")  # the user thinks again and drops the waiting message
        assert await wait_until(pilot, lambda: not app._delivery.pending)
        app.gate.set()
        assert await wait_until(pilot, lambda: script.calls == 3 and idle(app))
        approver = app._approver
        assert not approver.once and not app._pickups and ran == ["sleep"], "the dropped message allowed nothing"
        assert [p.detail for p in approver.parked] == [KEYS], "and its step waits again instead of vanishing"
        app._set_permission_mode("careful")
        await say(pilot, app, "check the settings")
        assert await wait_until(pilot, lambda: card(app) is not None), "the same command asks: no pass was left behind"
        assert card(app)._parked_at is None and KEYS not in ran
        # The card does not take the keyboard by itself. The user comes to it; a warned call is on Deny.
        assert isinstance(app.focused, ChatInput)
        await come_to_card(pilot, app)
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: idle(app) and script.calls == 5)
        assert KEYS not in ran
    assert_history_api_valid(engine.history)

    # ── one pick-up turn does not spend another's pass ─────────────────────────
    app, engine, ran, script = build([[A, B, C], f"$ {A}", "ran the first.", f"$ {B}", "ran the second."])
    async with app.run_test(size=(100, 34)) as pilot:
        await say(pilot, app, "look around")
        assert await wait_until(pilot, lambda: idle(app) and len(app._approver.parked) == 3)
        approver = app._approver
        script.hold = asyncio.Event()
        hold = script.hold
        await open_card(pilot, app)
        await pilot.press("up", "up", "enter")                       # A: run it now
        assert await wait_until(pilot, lambda: card(app) is not None and len(approver.parked) == 2)
        await pilot.press("escape")                  # B: later → A's pick-up starts and waits for the model
        assert await wait_until(pilot, lambda: script.calls == 2 and not idle(app))
        await open_card(pilot, app)
        await pilot.press("up", "up", "enter")                       # B: run it now, decided while A's turn runs
        assert await wait_until(pilot, lambda: card(app) is not None and len(approver.parked) == 1)
        hold.set()                                   # A's turn finishes while C's card is still open
        assert await wait_until(pilot, lambda: ran == [A] and script.calls == 3)
        assert card(app) is not None, "C's card is still open"
        await pilot.press("escape")                  # C: later → B's pick-up goes out
        assert await wait_until(pilot, lambda: ran == [A, B] and idle(app)), (ran, script.calls)
        assert [p.detail for p in approver.parked] == [C] and not approver.once
    assert_history_api_valid(engine.history)

    # ── a card that asks while the parked card is open waits its turn: the keys stay where the user is ─
    app, engine, ran, script = build([f"$ {KEYS}", "one waits.", "$ rm -rf build", "kept."])
    async with app.run_test(size=(100, 34)) as pilot:
        await say(pilot, app, "check the settings")
        assert await wait_until(pilot, lambda: script.calls >= 2 and idle(app))
        app._set_permission_mode("ask")
        script.hold = asyncio.Event()
        hold = script.hold
        await say(pilot, app, "clean up")
        assert await wait_until(pilot, lambda: script.calls == 3)
        await open_card(pilot, app)                  # the parked card, opened while the turn waits for the model
        assert card(app)._parked_at is not None
        parked_card = card(app)
        hold.set()                                   # the model wants `rm -rf build`: ask mode asks
        assert await wait_until(pilot, lambda: len(app.query(InlineApproval)) == 2)
        asking = app.query(InlineApproval).last()
        await pilot.pause(0.2)
        assert asking._parked_at is None and app.focused is parked_card, \
            "the card that asks does not take the keyboard from the card the user is on"
        # The user clicks the card that asks. The parked card makes way ("later"), and the
        # keyboard stays on the card they clicked: closing the parked one does not take it back.
        await pilot.click(asking.query_one("#perm-cmd"))
        assert await wait_until(pilot, lambda: len(app.query(InlineApproval)) == 1 and app.focused is asking), \
            "the keyboard left the card the user clicked"
        await pilot.pause(0.3)
        assert app.focused is asking and not asking._future.done()
        assert len(app._approver.parked) == 1 and not idle(app), "the parked step is still parked, the turn still waits"
        await pilot.press("down", "down", "enter")
        assert await wait_until(pilot, lambda: idle(app) and card(app) is None)
        assert ran == [] and isinstance(app.focused, ChatInput)
    assert_history_api_valid(engine.history)

    # ── ask mode is unchanged: the card comes at once, and its rows are clickable ─
    app, engine, ran, script = build([f"$ {KEYS}", "read it."], mode="ask")
    async with app.run_test(size=(100, 34)) as pilot:
        await say(pilot, app, "check the settings")
        assert await wait_until(pilot, lambda: card(app) is not None), "ask mode asks"
        asked = card(app)
        assert [value for value, _ in asked._choices] == ["once", "deny"], "no session grant for this step"
        assert not app._approver.parked and not app.query_one("#parkchip", Static).display
        await pilot.click("#perm-opt-0")
        assert await wait_until(pilot, lambda: ran == [KEYS] and idle(app)), "clicking Run once runs it"
    assert_history_api_valid(engine.history)

    print("TUI PARKED SMOKE OK — yolo not wait: skip, count, human decide later, exact pass, three strike stop. amaze!")


asyncio.run(main())
