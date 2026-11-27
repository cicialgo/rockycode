"""Typing is always the user's message (headless Textual pilot, no API).

The bug this guards: a card pops up in the chat while the user is in the middle
of a sentence — rocky wants to run a command, a plan is ready — and the card took
the keyboard. The next keys were read as its answer: Enter or `y` ran a command
nobody had looked at, and the rest of the sentence was dropped.

The rule (rockycode/tui/cardkeys.py), for every card in the chat:

  - a card the user did not open never takes the keyboard; the user comes to it
    with Enter in an empty message box, Tab, or a click. A card they opened (a
    command, a click) has the keyboard at once;
  - ↑ and ↓ in an empty message box do nothing while a card waits, but the card
    then has no picked row when the user comes to it: Enter never picks a row
    after arrows the card did not see;
  - no letter answers a card; a card is answered with ↑↓ + Enter, or a click;
  - what is typed or pasted on a card lands in the message box;
  - Enter is heard only after the card has had the keyboard, untouched, for half
    a second, whichever way the Enter or the keyboard got there. The arrows are
    never held back: Enter picks the row the arrows left highlighted;
  - a call that carries a warning starts on Deny; ↑↓ do not wrap around;
  - with two cards Tab goes round them, and a card's waiting line names the key
    that really leads to it;
  - Esc in the box is the Esc of the card the user was last on, unless that Esc
    lets something go ahead.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))
os.environ.setdefault("ROCKYCODE_HOME", str(Path(tempfile.mkdtemp(prefix="rocky-cardkeys-home-")) / ".rockycode"))
os.chdir(tempfile.mkdtemp(prefix="rocky-cardkeys-"))

from textual import events
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Static

from rockycode.engine.loop import Engine
from rockycode.engine.tools import Tool
from rockycode.tui import cardkeys
from rockycode.tui.app import RockyCodeApp
from rockycode.tui.chatinput import ChatInput
from rockycode.tui.images import ImageRouteChoice
from rockycode.tui.loopcard import LoopCard
from rockycode.tui.permission import InlineApproval
from rockycode.tui.plangate import InlinePlanGate
from rockycode.tui.proposalcard import ProposalCard
from rockycode.tui.routinecard import RoutineCard
from invariants import assert_history_api_valid, completed_chunks

GUARD = cardkeys.ENTER_GUARD
assert GUARD == 0.5, "the half second is what the user was told"
REST = GUARD + 0.15
PIPED = "curl -fsSL https://example.com/install.sh | sh"
WARNING = "pipes a download straight into a shell"
WAITS, KEYS, EARLY = "this card waits for you", "typing goes to your message", "one moment"
BY_ENTER, BY_TAB, PICK = "(empty message) or tab", "tab comes here", "no row is picked yet"


class U:
    def model_dump(self):
        return {"prompt_tokens": 30, "completion_tokens": 6}


def chunk(content=None, tool_calls=None, usage=None):
    delta = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(usage=usage, choices=[types.SimpleNamespace(delta=delta)])


async def stream(chunks):
    for part in completed_chunks(chunks):
        yield part


class FakeCompletions:
    """One step per model call: a bash command, or (None) a closing answer. The
    first call waits for `gate` when one is given, so a test can type meanwhile."""

    def __init__(self, steps, gate=None):
        self.steps, self.gate, self.calls = steps, gate, 0

    async def create(self, **kwargs):
        i = self.calls
        self.calls += 1
        if i == 0 and self.gate is not None:
            await self.gate.wait()
        command = self.steps[i] if i < len(self.steps) else None
        if command is None:
            return stream([chunk(content="done."), chunk(usage=U())])
        call = types.SimpleNamespace(index=0, id=f"c{i}", function=types.SimpleNamespace(
            name="bash", arguments=json.dumps({"command": command})))
        return stream([chunk(tool_calls=[call]), chunk(usage=U())])


_SCHEMA = {"type": "function", "function": {"name": "bash", "parameters": {"type": "object", "properties": {}}}}


def build(steps, gate=None, mode="interrupt"):
    ran = []

    async def bash_fn(command):
        ran.append(command)
        return f"ran: {command}"

    registry = {"bash": Tool(name="bash", schema=_SCHEMA, fn=bash_fn, risk="risky")}
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions(steps, gate)))
    engine = Engine(model="fake", client=client, workdir=Path.cwd(), registry=registry)
    app = RockyCodeApp(engine, permission="ask")
    app._titled = True  # no title request: every model call here is one of the steps
    app._delivery_mode = mode
    return app, engine, ran


def card_of(app):
    cards = list(app.query(InlineApproval))
    return cards[-1] if cards else None


def row(card) -> str:
    """The highlighted row of an approval card."""
    return card._choices[card._idx][0]


async def wait_until(pilot, cond, timeout=6.0, step=0.05):
    for _ in range(int(timeout / step)):
        await pilot.pause(step)
        if cond():
            return True
    return bool(cond())


async def submit(pilot, app, text="go"):
    box = app.query_one(ChatInput)
    box.focus()
    box.text = text
    await pilot.press("enter")


def keys_line(card) -> str:
    return str(card.query_one(card.KEYS_ID, Static).render())


def drawn(card) -> int:
    """The row the card DRAWS as highlighted (-1: none), read from the screen, not from its state."""
    rows = [w for w in card.query(Static) if "-opt-" in (w.id or "")]
    lit = [i for i, w in enumerate(rows) if "▸" in str(w.render())]
    assert len(lit) <= 1, lit
    return lit[0] if lit else -1


async def a_card_opens(pilot, app):
    """The turn asks. The card is there, and the keyboard is still in the message box."""
    assert await wait_until(pilot, lambda: card_of(app) is not None)
    card, box = card_of(app), app.query_one(ChatInput)
    await pilot.pause(0.3)
    assert app.focused is box and not card.has_focus, "the card took the keyboard by itself"
    assert WAITS in keys_line(card), keys_line(card)
    return card, box


async def come(pilot, app, card, key="enter"):
    """The user comes to the card: Enter in the empty message box, or Tab."""
    await pilot.press(key)
    assert await wait_until(pilot, lambda: app.focused is card, timeout=2.0), f"{key} did not bring the keyboard to the card"


async def on_card(pilot, app, card, key):
    """One key pressed while the card has the keyboard (a click on it first, if it lost it)."""
    if app.focused is not card:
        card.focus()
        assert await wait_until(pilot, lambda: app.focused is card, timeout=2.0)
    await pilot.press(key)
    await pilot.pause()


async def never_takes_the_keyboard():
    """Nobody is typing, the box is empty, and still the card waits for the user to come to it."""
    app, engine, ran = build(["ls -la"])
    async with app.run_test(size=(100, 34)) as pilot:
        await submit(pilot, app)
        card, box = await a_card_opens(pilot, app)
        for key in "yes any":  # the sentence the user was starting: every key is the message
            await pilot.press("space" if key == " " else key)
        await pilot.pause()
        assert box.text == "yes any" and app.focused is box and not card._future.done() and not ran
        box.text = ""
        await pilot.pause(0.2)
        assert app.focused is box, "the box emptied and the card took the keyboard"
        assert BY_ENTER in keys_line(card)
        line = card.query_one(card.KEYS_ID, Static)
        assert line.styles.min_height.value >= 3 and line.size.height == 3, "the waiting card keeps room for its three key lines"
        # Enter with nothing to send goes to the card. It brings the keyboard; it answers nothing.
        await come(pilot, app, card)
        assert KEYS in keys_line(card) and not card._future.done()
        for _ in range(5):  # the same Enter held down, or pressed again and again
            await pilot.press("enter")
            await pilot.pause(GUARD * 0.5)
            assert not card._future.done() and not ran and EARLY in keys_line(card), keys_line(card)
        await pilot.pause(REST)
        assert KEYS in keys_line(card), "the card's keys are back on its last line"
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: ran == ["ls -la"]), "the keyboard rested on the card: Enter answers"
        assert await wait_until(pilot, lambda: app.focused is box and card_of(app) is None)
    assert_history_api_valid(engine.history)
    print("a card never takes the keyboard: Enter in an empty message goes to it, and only a later Enter answers  ✓")


async def typed_on_the_card():
    """The user came to the card, then starts a sentence after all."""
    app, engine, ran = build(["ls -la"])
    async with app.run_test(size=(100, 34)) as pilot:
        await submit(pilot, app)
        card, box = await a_card_opens(pilot, app)
        await come(pilot, app, card)
        await pilot.press("space")
        await pilot.pause()
        assert box.text == "" and app.focused is card, "a blank at the start is not a message: it would only hide in the box"
        for key in "no wait":
            await pilot.press("space" if key == " " else key)
        await pilot.pause()
        assert box.text == "no wait" and app.focused is box and not card._future.done() and not ran
        assert WAITS in keys_line(card)
        # Tab goes to the card and back; a paste on the card is typing too.
        await come(pilot, app, card, "tab")
        app.post_message(events.Paste(" more"))
        assert await wait_until(pilot, lambda: box.text == "no wait more" and app.focused is box)
        await come(pilot, app, card, "tab")
        await pilot.press("tab")
        assert await wait_until(pilot, lambda: app.focused is box), "tab on the card comes back to the message"
        await pilot.pause(0.2)
        assert app.focused is box, "the keyboard stays where the user put it"
        await come(pilot, app, card, "tab")
        await pilot.press("enter")  # tab, Enter: the reflex after a completion must not run the call
        await pilot.pause(0.1)
        assert not card._future.done() and not ran and EARLY in keys_line(card)
        await pilot.pause(REST)
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: ran == ["ls -la"])
        assert await wait_until(pilot, lambda: app.focused is box and card_of(app) is None)
        assert box.text == "no wait more", "the draft is still there after the card"
    assert_history_api_valid(engine.history)
    print("typing on a card: letters and a paste go to the message, tab moves both ways, tab then Enter is not an answer  ✓")


async def card_while_typing(mode):
    """The user is mid-sentence when rocky wants to run a command."""
    gate = asyncio.Event()
    app, engine, ran = build(["ls -la", None, None], gate, mode)
    async with app.run_test(size=(100, 34)) as pilot:
        await submit(pilot, app)
        for key in "fix":
            await pilot.press(key)
        gate.set()
        card, box = await a_card_opens(pilot, app)
        await pilot.press("e", "d")
        await pilot.press("enter")  # sends "fixed", as Enter in the message box always does
        await pilot.pause()
        assert not ran, "the Enter that sent the message did not run the command"
        if mode == "interrupt":
            # rocky's default: a message sent while it works takes over the turn. The
            # waiting call is dropped with the turn — it is never run.
            assert await wait_until(pilot, lambda: card_of(app) is None)
            assert await wait_until(pilot, lambda: any(
                m["role"] == "user" and "fixed" in str(m["content"]) for m in engine.history))
            await pilot.pause(REST)
            assert not ran and app.focused is box
        else:
            # /queue on — the message waits its turn and the card is still there, still waiting.
            await pilot.pause(REST)
            assert app.focused is box and box.text == "" and not card._future.done() and not ran
            await come(pilot, app, card)
            await pilot.pause(REST)
            await pilot.press("enter")
            assert await wait_until(pilot, lambda: ran == ["ls -la"])
            assert await wait_until(pilot, lambda: any(
                m["role"] == "user" and "fixed" in str(m["content"]) for m in engine.history)), "the message ran next"
    assert_history_api_valid(engine.history)
    print(f"a card while typing (send: {mode}): Enter sends the message, the command does not run by it  ✓")


async def arrows_are_never_held_back():
    """Enter picks the row the arrows left: an arrow is never dropped while the Enter behind it is kept."""
    # Deny with ↓ ↓ Enter, pressed the moment the user is on the card.
    app, engine, ran = build(["ls -la"])
    async with app.run_test(size=(100, 34)) as pilot:
        await submit(pilot, app)
        card, box = await a_card_opens(pilot, app)
        await come(pilot, app, card)
        await pilot.press("down")
        await pilot.pause()
        assert row(card) == "session", "an arrow in the card's first half second was dropped"
        await pilot.pause(REST)  # the half second since the user came is over …
        await pilot.press("down")
        await pilot.pause()
        assert row(card) == "deny"
        await pilot.press("enter")  # … but not the one since the arrow: not heard, and nothing moved
        await pilot.pause(0.1)
        assert not card._future.done() and row(card) == "deny" and EARLY in keys_line(card), \
            "Enter right behind an arrow answered"
        await pilot.press("down")  # the last row, no draft below: it stays
        await pilot.pause()
        assert row(card) == "deny" and app.focused is card
        await pilot.pause(REST)
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: card._future.done() and card._future.result() == "deny")
        assert await wait_until(pilot, lambda: any(m["role"] == "tool" for m in engine.history))
        assert not ran
    assert_history_api_valid(engine.history)
    # Two calls in one turn, both denied with ↓ ↓ Enter … Enter, pressed without coming to the
    # card first. The arrows are the message box's: they move nothing. But the card was told,
    # so it comes up with no row picked, and Enter picks nothing — not the row it started on.
    app, engine, ran = build(["rm -rf build", "rm -rf dist"])
    async with app.run_test(size=(100, 34)) as pilot:
        await submit(pilot, app)
        for n in (1, 2):
            assert await wait_until(pilot, lambda: card_of(app) is not None and not card_of(app)._future.done())
            card, box = await a_card_opens(pilot, app)
            await pilot.press("down", "down")
            await pilot.pause(0.1)
            assert app.focused is box and card._idx == 0, f"call {n}: arrows in the message box moved the card"
            await come(pilot, app, card)
            assert drawn(card) == -1 and PICK in keys_line(card), "arrows the card did not see: no row is picked"
            for _ in range(3):  # Enter, Enter, Enter: early or not, nothing is picked
                await pilot.press("enter")
                await pilot.pause(REST)
                assert not card._future.done() and not ran, f"call {n}: Enter picked a row the user did not choose"
            assert PICK in keys_line(card)
            await pilot.press("tab")  # away and back: still no row
            assert await wait_until(pilot, lambda: app.focused is box, timeout=2.0)
            await come(pilot, app, card)
            assert drawn(card) == -1 and PICK in keys_line(card)
            await pilot.press("down")  # the first arrow on the card only shows the row it stands on
            await pilot.pause()
            assert row(card) == "once" and drawn(card) == 0 and KEYS in keys_line(card)
            await pilot.press("down", "down")  # from there the arrows move, as always
            await pilot.pause(REST)
            assert drawn(card) == 2
            await pilot.press("enter")
            assert await wait_until(pilot, lambda: card._future.done() and card._future.result() == "deny"), n
            assert await wait_until(pilot, lambda: card_of(app) is not card)
        assert await wait_until(pilot, lambda: any(m["role"] == "assistant" and "done." in str(m.get("content")) for m in engine.history))
        assert not ran, "a call the user denied with the arrows ran"
    assert_history_api_valid(engine.history)
    # The same for any arrow in the empty box: ↑ for the last message (it brings back none while a
    # card waits), ↑ then ↓ to take it back, one ↓ too many on the way out of the message history.
    gate = asyncio.Event()
    app, engine, ran = build([PIPED], gate)
    async with app.run_test(size=(100, 34)) as pilot:
        await submit(pilot, app, "an older message")
        box = app.query_one(ChatInput)
        await pilot.press("up")  # browsing the message history when the card opens
        await pilot.pause()
        assert box.text == "an older message"
        gate.set()
        assert await wait_until(pilot, lambda: card_of(app) is not None)
        card = card_of(app)
        await pilot.pause(0.3)
        assert row(card) == "deny" and not card._poked
        await pilot.press("down")  # back to the empty draft: the box's own key, and the card is told
        await pilot.pause()
        assert box.text == "" and app.focused is box and card._poked
        await pilot.press("down")  # … and one press too many
        await pilot.press("up")    # reaching for "run once", or for the last message: neither
        await pilot.pause(0.1)
        assert app.focused is box and box.text == "" and row(card) == "deny" and card._poked
        await come(pilot, app, card)
        assert drawn(card) == -1 and PICK in keys_line(card)
        await pilot.pause(REST)
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert not card._future.done() and not ran
        await pilot.press("up")  # the first arrow shows where the card stands: on Deny. It does not move to "run".
        await pilot.pause(REST)
        assert row(card) == "deny" and drawn(card) == card._idx and KEYS in keys_line(card)
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: card._future.done() and card._future.result() == "deny")
        assert not ran
    assert_history_api_valid(engine.history)
    # ↓ past the last row goes back to a message that is being written; a scroll key restarts the wait.
    gate = asyncio.Event()
    app, engine, ran = build(["ls -la", None, None], gate, "queue")
    async with app.run_test(size=(100, 34)) as pilot:
        await submit(pilot, app)
        for key in "also":
            await pilot.press(key)
        gate.set()
        card, box = await a_card_opens(pilot, app)
        await pilot.press("up")  # with a draft ↑ is the box's own key (the last message): not a way to the card
        await pilot.pause(0.1)
        assert app.focused is box and row(card) == "once" and box.text == "go", box.text
        box.text = "also"
        await come(pilot, app, card, "tab")
        # … and the card was told all the same: ↓ ↓ Tab Enter with a draft must not pick the start row
        assert drawn(card) == -1 and PICK in keys_line(card), "an arrow pressed in a draft was not remembered"
        await pilot.pause(REST)
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert not card._future.done() and not ran
        await pilot.press("down", "down", "down")  # show, move, move
        await pilot.pause()
        assert row(card) == "deny" and drawn(card) == 2 and app.focused is card
        await pilot.press("down")
        assert await wait_until(pilot, lambda: app.focused is box), "↓ past the last row goes back to the message"
        assert row(card) == "deny" and not card._future.done() and box.text == "also"
        await come(pilot, app, card, "tab")
        await pilot.pause(REST)
        await pilot.press("home")
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert not card._future.done(), "Enter right behind another key answered"
        await pilot.pause(REST)
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: card._future.done() and card._future.result() == "deny")
        assert not ran
    print("arrows act at once and Enter picks their row; keys pressed before the user came to a card move nothing  ✓")


async def esc_is_the_cards():
    """After one stray letter the keyboard is in the box; Esc there is still the card's Esc."""
    app, engine, ran = build(["ls -la"])
    async with app.run_test(size=(100, 34)) as pilot:
        await submit(pilot, app)
        card, box = await a_card_opens(pilot, app)
        await come(pilot, app, card)
        await pilot.press("n")  # the old "deny" letter: now it is a letter
        await pilot.pause()
        assert box.text == "n" and app.focused is box and not card._future.done()
        await pilot.press("escape")
        assert await wait_until(pilot, lambda: card._future.done()), "Esc in the box reached the card"
        assert card._future.result() == "cancel" and not ran
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    host = Host()
    async with host.run_test(size=(100, 40)) as pilot:
        box = host.query_one(ChatInput)
        card = the_cards()["proposal"](future)
        await host.mount(card, before=box)
        assert await wait_until(pilot, lambda: host.focused is card, timeout=2.0)
        await pilot.press("x")
        await pilot.pause()
        assert host.focused is box and box.text == "x"
        await pilot.press("escape")
        await pilot.pause()
        assert future.done() and future.result() == "later", "a proposal's Esc is \"later\", from the box too"
    # The image card's Esc sends the message without its image: Esc in the box is not that Esc.
    future = loop.create_future()
    host = Host()
    async with host.run_test(size=(100, 40)) as pilot:
        box = host.query_one(ChatInput)
        card = the_cards()["image"](future)
        await host.mount(card, before=box)
        await pilot.pause(0.2)
        assert host.focused is not card
        await come(pilot, host, card)  # the user was on the card: it is the one Esc would go to
        await pilot.press("w")
        await pilot.pause()
        assert host.focused is box and box.text == "w" and cardkeys.last_on(host) is card
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not future.done(), "Esc in the box answered the image card: the message would go without its image"
    print("esc in the message box is the waiting card's esc; not the image card's, whose esc lets the message go  ✓")


async def warned_call():
    """A call that carries a warning starts on Deny; no arrow wraps around to "run"."""
    for keys, runs in (((), False), (("up",), True), (("down",), False)):
        app, engine, ran = build([PIPED])
        async with app.run_test(size=(100, 34)) as pilot:
            await submit(pilot, app)
            card, box = await a_card_opens(pilot, app)
            assert card._warning and row(card) == "deny", "the highlight starts on Deny"
            await come(pilot, app, card)
            assert row(card) == "deny", "coming to the card moved its highlight"
            for key in keys:
                await pilot.press(key)
                await pilot.pause()
            assert row(card) == ("once" if runs else "deny"), "↓ on the last row stays there: no wrap-around"
            await pilot.pause(REST)
            await pilot.press("enter")
            assert await wait_until(pilot, lambda: card_of(app) is None)
            assert await wait_until(pilot, lambda: any(m["role"] == "tool" for m in engine.history))
            assert (ran == [PIPED]) is runs, (keys, ran)
        assert_history_api_valid(engine.history)
    app, engine, ran = build(["ls -la"])
    async with app.run_test(size=(100, 34)) as pilot:
        await submit(pilot, app)
        card, box = await a_card_opens(pilot, app)
        assert not card._warning and row(card) == "once", "an ordinary call still starts on run once"
        await come(pilot, app, card)
        await pilot.press("up")  # the first row: ↑ stays, it does not wrap to Deny
        await pilot.pause(REST)
        assert row(card) == "once"
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: ran == ["ls -la"])
    print("a warned call starts on Deny and no arrow wraps to run; an ordinary one starts on run once  ✓")


# ── the cards, each on its own in a bare chat ────────────────────────────────
class Line(Static):
    can_focus = True


class Host(App):
    def compose(self):
        yield VerticalScroll(id="transcript", can_focus=False)
        yield ChatInput(id="prompt")


def the_cards():
    proposal = types.SimpleNamespace(kind="skill", name="tidy", reason="seen twice", evidence=["a.jsonl"], body="steps")
    routine = types.SimpleNamespace(auto=False, budget_lease=2.5, name="nightly", description="tests", prompt="run",
                                    tools=["bash"], cadence="daily", network=False, max_steps=8)
    record = types.SimpleNamespace(status="paused", blocked_on={"detail": "git push"}, ticks=0, prompt="watch ci",
                                   interval_s=60, for_s=None, count=None, max_spend=2, spend=0.1, last_tick=0, id=3)
    return {
        "approval": lambda f: InlineApproval("bash", "ls -la", "risky", None, f, session_label="allow ls"),
        "warned": lambda f: InlineApproval("bash", PIPED, "risky", WARNING, f),
        "plan": lambda f: InlinePlanGate("plan.md", "# the plan\n" + "- a step\n" * 4, f),
        "image": lambda f: ImageRouteChoice(1, "kimi:kimi-k3", "", f),
        "parked": lambda f: InlineApproval("bash", "ls -la", "risky", None, f, parked_at=time.time()),
        "loop allow": lambda f: InlineApproval("bash", "git push", "risky", None, f, invited=True),
        "plan, asked for": lambda f: InlinePlanGate("plan.md", "# the plan\n- a step\n", f, invited=True),
        "loop": lambda f: LoopCard(record, "$", f),
        "proposal": lambda f: ProposalCard(proposal, f),
        "routine": lambda f: RoutineCard(routine, 0.25, f),
    }


LETTERS = "yangeps"  # every letter that used to answer one of these cards
OPENS_ITSELF = ("approval", "warned", "plan", "image")


async def no_letter_answers():
    loop = asyncio.get_running_loop()
    for name, make in the_cards().items():
        future = loop.create_future()
        app = Host()
        async with app.run_test(size=(100, 44)) as pilot:
            box = app.query_one(ChatInput)
            assert await wait_until(pilot, lambda: app.focused is box, timeout=2.0)
            card = make(future)
            await app.mount(card, before=box)
            await pilot.pause(0.2)
            assert (app.focused is card) is (name not in OPENS_ITSELF), \
                f"{name}: {'it opened by itself' if name in OPENS_ITSELF else 'the user asked for it'}"
            bound = {b[0] if isinstance(b, tuple) else b.key for b in card.BINDINGS}
            assert not bound & set(LETTERS), f"{name}: a letter is still bound: {bound & set(LETTERS)}"
            if name == "parked":  # open only while it has the keyboard: typing means "later"
                await on_card(pilot, app, card, LETTERS[0])
                assert future.result() == "later" and box.text == LETTERS[0]
                continue
            for n, letter in enumerate(LETTERS, 1):
                await on_card(pilot, app, card, letter)  # each letter is pressed ON the card
                assert not future.done(), f"{name}: {letter!r} answered the card"
                assert box.text == LETTERS[:n] and app.focused is box, (name, letter, box.text)
            # Deleted: the card does not take the keyboard back. Enter with nothing to send goes to
            # it, whichever card it is, and only an Enter half a second later answers.
            box.text = ""
            await pilot.pause(0.2)
            assert app.focused is box, f"{name}: the box emptied and the card took the keyboard"
            await come(pilot, app, card)
            await pilot.press("enter")
            await pilot.pause()
            assert not future.done(), f"{name}: the Enter behind the one that came to the card answered"
            await pilot.pause(REST)
            await pilot.press("enter")
            await pilot.pause()
            assert future.done(), f"{name}: Enter answers after the half second"
    print("ten cards: the ones the user did not open wait, no letter answers any, the letters are the message  ✓")


async def rows_click_and_a_draft_is_safe():
    loop = asyncio.get_running_loop()
    cards = the_cards()
    for name in cards:
        future = loop.create_future()
        app = Host()
        async with app.run_test(size=(100, 44)) as pilot:
            box = app.query_one(ChatInput)
            box.focus()
            box.text = "half a sentence"
            await pilot.pause()
            card = cards[name](future)
            await app.mount(card, before=box)
            await pilot.pause(0.2)
            itself = name in OPENS_ITSELF
            assert (app.focused is card) is not itself, name
            if not itself:
                continue
            await pilot.press("enter")  # the message box's Enter: it sends, it does not answer
            await pilot.pause()
            assert not future.done(), f"{name}: the Enter that sent the message answered the card"
            # Every row is a place to click, on a card that waits too: it keeps its height when
            # the click brings the keyboard, so the row is still under the mouse.
            target, picked = {"approval": ("#perm-opt-2", "deny"), "warned": ("#perm-opt-0", "once"),
                              "plan": ("#plan-opt-1", "approve"), "image": ("#route-opt-1", "provider_always")}[name]
            await pilot.click(target)
            await pilot.pause()
            assert future.done() and future.result() == picked, (name, target, future.done())
    # A click in the plan's text does not take the keyboard off the card.
    future = loop.create_future()
    app = Host()
    async with app.run_test(size=(100, 44)) as pilot:
        card = cards["plan"](future)
        await app.mount(card, before=app.query_one(ChatInput))
        await pilot.pause(0.1)
        card.focus()
        assert await wait_until(pilot, lambda: app.focused is card, timeout=2.0)
        await pilot.click("#plan-preview")
        await pilot.pause(0.1)
        assert app.focused is card and KEYS in keys_line(card)
    print("with a draft in the box a card waits and Enter sends the draft; every row of a waiting card clicks  ✓")


async def another_card_has_the_keyboard():
    """The user is on a card (↓ ↓ Enter) when an approval opens: those keys are not the approval's.
    When their card closes, the keyboard goes to the message box, not to the card that waits."""
    loop = asyncio.get_running_loop()
    cards = the_cards()
    for approval_first in (False, True):  # the approval opens under the user's card, or was there above it
        first, second = loop.create_future(), loop.create_future()
        app = Host()
        async with app.run_test(size=(100, 48)) as pilot:
            box = app.query_one(ChatInput)
            routine, approval = cards["routine"](first), cards["approval"](second)
            for card in ((approval, routine) if approval_first else (routine, approval)):
                await app.mount(card, before=box)
                await pilot.pause(0.2)
            assert await wait_until(pilot, lambda: app.focused is routine, timeout=2.0)
            assert not approval.has_focus and BY_TAB in keys_line(approval)
            await pilot.pause(REST)
            await pilot.press("down", "down")
            await pilot.pause(REST)
            await pilot.press("enter")
            await pilot.pause()
            assert first.result() == "later" and not second.done(), "the keys were the routine card's"
            # The app closes the answered card. Left to itself Textual hands the keyboard to the
            # card above, which can be the one that waits: it goes back to the message box.
            cardkeys.card_closing(app, routine)
            await routine.remove()
            await pilot.pause(REST)
            assert app.focused is box and not approval.has_focus, \
                f"a card closed (approval {'above' if approval_first else 'below'}): the keyboard goes to the box"
            assert cardkeys.last_on(app) is None, "the card that waits became the one the user was on"
            assert not second.done() and BY_ENTER in keys_line(approval), keys_line(approval)
    first, second = loop.create_future(), loop.create_future()
    app = Host()
    async with app.run_test(size=(100, 48)) as pilot:
        box = app.query_one(ChatInput)
        routine = cards["routine"](first)
        await app.mount(routine, before=box)
        assert await wait_until(pilot, lambda: app.focused is routine, timeout=2.0)
        await pilot.pause(REST)
        approval = cards["approval"](second)
        await app.mount(approval, before=box)
        await pilot.pause(0.2)
        assert app.focused is routine and WAITS in keys_line(approval)
        await pilot.press("down", "down")
        await pilot.pause(REST)
        await pilot.press("enter")
        await pilot.pause()
        assert first.result() == "later" and not second.done(), "the keys were the routine card's"
        # The app closes the answered card. Left to itself Textual would hand the keyboard to
        # a neighbour, which can be the card that waits: it goes back to the message box.
        cardkeys.card_closing(app, routine)
        await routine.remove()
        await pilot.pause(REST)
        assert app.focused is box and not approval.has_focus, "a card closed: the keyboard goes to the box"
        assert not second.done() and cardkeys.waiting(app) is approval and BY_ENTER in keys_line(approval)
        for _ in range(6):  # the Enter that answered the first card, still coming: it only comes to the next
            await pilot.press("enter")
            await pilot.pause(0.02)
        assert app.focused is approval and not second.done(), "the Enter that answered one card answered the next"
        await pilot.pause(REST)
        await pilot.press("enter")
        await pilot.pause()
        assert second.result() == "once"
    # The same close with nothing done about the keyboard shows why: this is what Textual does alone.
    first, second = loop.create_future(), loop.create_future()
    app = Host()
    async with app.run_test(size=(100, 48)) as pilot:
        box = app.query_one(ChatInput)
        approval, routine = cards["approval"](second), cards["routine"](first)
        await app.mount(approval, before=box)
        await app.mount(routine, before=box)
        assert await wait_until(pilot, lambda: app.focused is routine, timeout=2.0)
        routine.pick("later")
        await routine.remove()
        await pilot.pause(0.3)
        alone = app.focused
        approval.action_confirm()
        assert not second.done(), "a card that got the keyboard from Textual heard Enter at once"
    print(f"  (left alone, Textual hands a closed card's keyboard to: {type(alone).__name__ if alone is not None else 'nothing'})")
    print("an approval that opens while the user is on another card: their keys stay theirs, and it waits after too  ✓")


async def the_wait_is_in_the_action():
    """Enter reaches a card through its binding, from whatever widget let the key through: the
    half second is checked where the answer is given, and a card without the keyboard is deaf."""
    loop = asyncio.get_running_loop()
    cards = the_cards()
    for name in ("approval", "plan", "image", "loop", "proposal", "routine"):
        future = loop.create_future()
        app = Host()
        async with app.run_test(size=(100, 44)) as pilot:
            box = app.query_one(ChatInput)
            card = cards[name](future)
            await app.mount(card, before=box)
            await pilot.pause(0.2)
            box.focus()
            assert await wait_until(pilot, lambda: app.focused is box, timeout=2.0)
            await pilot.pause(0.1)
            card.action_confirm()  # an Enter that was never this card's
            await pilot.pause(REST)
            card.action_confirm()
            assert not future.done(), f"{name}: answered without the keyboard"
            card.focus()  # the keyboard arrives some other way (a click, Textual moving focus on)
            card.action_confirm()  # …and an Enter before the card even knows
            assert await wait_until(pilot, lambda: app.focused is card, timeout=2.0)
            card.action_confirm()
            assert not future.done(), f"{name}: answered in the half second after it got the keyboard"
            await pilot.pause(REST)
            card.action_confirm()
            assert future.done(), f"{name}: the keyboard rested on it"
    print("the half second holds whichever way Enter or the keyboard comes; a card without the keyboard is deaf  ✓")


async def keys_that_write_without_a_character():
    """ctrl+j, ctrl+v, Backspace and Delete on a card are the message's; Enter on blanks sends nothing."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    app = Host()
    async with app.run_test(size=(100, 44)) as pilot:
        box = app.query_one(ChatInput)
        card = the_cards()["approval"](future)
        await app.mount(card, before=box)
        await pilot.pause(0.2)
        await on_card(pilot, app, card, "ctrl+j")
        assert app.focused is box and box.text == "\n", "ctrl+j on a card is a new line in the message"
        # Only blanks in the box: Enter empties it, and does not go on to the card in the same press.
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert box.text == "" and app.focused is box and not future.done()
        box.text = "ab"
        box.move_cursor((0, 0))
        await on_card(pilot, app, card, "delete")
        assert app.focused is box and box.text == "b", f"delete on a card edits the message: {box.text!r}"
        box.move_cursor(box.document.end)
        await on_card(pilot, app, card, "backspace")
        assert app.focused is box and box.text == "", "backspace on a card edits the message"
        requested = []
        real = box.post_message
        box.post_message = lambda message: (requested.append(type(message).__name__), real(message))[1]
        await on_card(pilot, app, card, "ctrl+v")
        assert "PasteRequested" in requested and app.focused is box, "ctrl+v on a card asks the box to paste"
        assert not future.done()
    print("ctrl+j, ctrl+v, backspace and delete on a card write the message; Enter on blanks only empties the box  ✓")


async def the_last_line_says_who_has_the_keyboard():
    """Keys while the card has the keyboard, the way to it while it has not — also when the user
    is in another window. The card keeps its height through all of it."""
    app, engine, ran = build(["ls -la"])
    async with app.run_test(size=(100, 34)) as pilot:
        await submit(pilot, app)
        card, box = await a_card_opens(pilot, app)
        height = card.size.height
        await come(pilot, app, card)
        assert KEYS in keys_line(card) and card.size.height == height
        app.post_message(events.AppBlur())  # the user looks at another window
        assert await wait_until(pilot, lambda: not app.app_focus, timeout=2.0)
        await pilot.pause(0.2)
        assert WAITS in keys_line(card) and card.size.height == height
        card.action_confirm()
        assert not card._future.done(), "a card heard Enter while the user was in another window"
        app.post_message(events.AppFocus())
        assert await wait_until(pilot, lambda: app.focused is card and KEYS in keys_line(card), timeout=2.0)
        await pilot.press("enter")  # back in the window: the half second starts again
        await pilot.pause(0.1)
        assert not card._future.done() and not ran and card.size.height == height
        await pilot.press("tab")
        assert await wait_until(pilot, lambda: app.focused is box and WAITS in keys_line(card))
        assert card.size.height == height
        await come(pilot, app, card)
        await pilot.pause(REST)
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: ran == ["ls -la"])
    print("the card's last line follows the keyboard, another window included, and the card keeps its height  ✓")


async def two_cards():
    """Two cards at once. Each can be reached by keyboard, a waiting line names the key that
    really leads to its card, and Esc from the box is the Esc of the card the user was on."""
    loop = asyncio.get_running_loop()
    cards = the_cards()
    # The user is on their own card when a turn asks: the new card must not say "↵".
    # Enter there is their own card's Enter.
    mine, asked = loop.create_future(), loop.create_future()
    app = Host()
    async with app.run_test(size=(100, 48)) as pilot:
        box = app.query_one(ChatInput)
        proposal = cards["proposal"](mine)
        await app.mount(proposal, before=box)
        assert await wait_until(pilot, lambda: app.focused is proposal, timeout=2.0)
        approval = cards["approval"](asked)
        await app.mount(approval, before=box)
        assert await wait_until(pilot, lambda: BY_TAB in keys_line(approval), timeout=2.0), keys_line(approval)
        assert "↵" not in keys_line(approval) and app.focused is proposal
        # One stray letter, then Esc meaning "later" for the card the user was on: the
        # approval they never came to is not cancelled by it.
        await pilot.press("x")
        await pilot.pause()
        assert app.focused is box and box.text == "x"
        assert BY_ENTER in keys_line(approval) and BY_TAB in keys_line(proposal), \
            "from the message box Enter leads to the newest card, Tab to the other"
        await pilot.press("escape")
        await pilot.pause()
        assert mine.done() and mine.result() == "later" and not asked.done()
    # Tab goes round: the message box, the newest card, the one above, the box again.
    # Esc from the box goes back along the cards the user was on, newest first.
    mine, asked = loop.create_future(), loop.create_future()
    app = Host()
    async with app.run_test(size=(100, 48)) as pilot:
        box = app.query_one(ChatInput)
        proposal, approval = cards["proposal"](mine), cards["approval"](asked)
        await app.mount(proposal, before=box)
        assert await wait_until(pilot, lambda: app.focused is proposal, timeout=2.0)
        await app.mount(approval, before=box)
        await pilot.pause(0.2)
        seen = []
        for _ in range(4):
            await pilot.press("tab")
            await pilot.pause(0.1)
            seen.append("box" if app.focused is box else "approval" if app.focused is approval else "proposal")
        assert seen == ["box", "approval", "proposal", "box"], seen
        await pilot.press("tab")  # to the approval: the user has now been on both cards
        assert await wait_until(pilot, lambda: app.focused is approval, timeout=2.0)
        assert BY_TAB in keys_line(proposal), "the card the user left says how to come back"
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert asked.done() and asked.result() == "cancel" and not mine.done(), "Esc is the last card's first"
        cardkeys.card_closing(app, approval)
        await approval.remove()
        await pilot.pause(0.1)
        assert BY_ENTER in keys_line(proposal), "one card left: Enter leads to it again"
        await pilot.press("escape")
        await pilot.pause()
        assert mine.done() and mine.result() == "later", "then the Esc of the card they were on before"
    # The keyboard is on something that is neither the box nor a card (a picker, a line of the
    # transcript): Enter there is that thing's Enter, so the waiting card does not say "↵".
    future = loop.create_future()
    app = Host()
    async with app.run_test(size=(100, 44)) as pilot:
        line = Line("a picker row")
        await app.query_one("#transcript").mount(line)
        line.focus()
        await pilot.pause(0.1)
        card = cards["approval"](future)
        await app.mount(card, before=app.query_one(ChatInput))
        assert await wait_until(pilot, lambda: BY_TAB in keys_line(card), timeout=2.0), keys_line(card)
        assert "↵" not in keys_line(card) and app.focused is line
    # Both wait and the keyboard is in the empty box: Enter comes to the newest, Tab goes on to the older.
    first, second = loop.create_future(), loop.create_future()
    app = Host()
    async with app.run_test(size=(100, 48)) as pilot:
        box = app.query_one(ChatInput)
        older, newer = cards["approval"](first), cards["plan"](second)
        await app.mount(older, before=box)
        await app.mount(newer, before=box)
        assert await wait_until(pilot, lambda: BY_ENTER in keys_line(newer) and BY_TAB in keys_line(older), timeout=2.0)
        assert app.focused is box
        await pilot.press("escape")  # the user came to neither: Esc here is not a card's
        await pilot.pause(0.1)
        assert not first.done() and not second.done()
        await come(pilot, app, newer)
        await pilot.press("tab")
        assert await wait_until(pilot, lambda: app.focused is older, timeout=2.0), "the older card has a keyboard way"
        await pilot.pause(REST)
        await pilot.press("enter")
        await pilot.pause()
        assert first.result() == "once" and not second.done()
    # The user clicks a waiting card while a parked step is open. The parked card closes
    # ("later"), and the keyboard stays on the card they clicked: it is not thrown to the box.
    first, second = loop.create_future(), loop.create_future()
    app = Host()
    async with app.run_test(size=(100, 48)) as pilot:
        box = app.query_one(ChatInput)
        approval, parked = cards["approval"](first), cards["parked"](second)
        await app.mount(approval, before=box)
        await app.mount(parked, before=box)
        assert await wait_until(pilot, lambda: app.focused is parked, timeout=2.0)
        await pilot.click("#perm-cmd")  # the approval's command text: the first one on screen
        assert await wait_until(pilot, lambda: second.done() and app.focused is approval, timeout=2.0)
        cardkeys.card_closing(app, parked)
        await parked.remove()
        await pilot.pause(0.2)
        assert second.result() == "later" and app.focused is approval and not first.done()
    # Arrows in the message box are remembered by every card that waits, not only the newest:
    # the user may come to the other one.
    first, second = loop.create_future(), loop.create_future()
    app = Host()
    async with app.run_test(size=(100, 48)) as pilot:
        box = app.query_one(ChatInput)
        older, newer = cards["approval"](first), cards["plan"](second)
        await app.mount(older, before=box)
        await app.mount(newer, before=box)
        await pilot.pause(0.3)
        await pilot.press("down", "down")
        await come(pilot, app, newer, "tab")
        await pilot.press("tab")
        assert await wait_until(pilot, lambda: app.focused is older, timeout=2.0)
        assert drawn(older) == -1 and PICK in keys_line(older), "only the newest card remembered the arrows"
        await pilot.pause(REST)
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert not first.done()
    # A waiting line follows the keyboard also when it goes to something that is not a card.
    future = loop.create_future()
    app = Host()
    async with app.run_test(size=(100, 44)) as pilot:
        box = app.query_one(ChatInput)
        line = Line("a picker row")
        await app.query_one("#transcript").mount(line)
        card = cards["approval"](future)
        await app.mount(card, before=box)
        assert await wait_until(pilot, lambda: BY_ENTER in keys_line(card), timeout=2.0), keys_line(card)
        line.focus()
        assert await wait_until(pilot, lambda: BY_TAB in keys_line(card), timeout=2.0), "the box lost the keyboard: ↵ no longer leads here"
        box.focus()
        assert await wait_until(pilot, lambda: BY_ENTER in keys_line(card), timeout=2.0), "the box has it again"
    # A draft that starts with "/" keeps Tab for its completion; the floor of the line's height
    # does not depend on a measurement that may not be there yet.
    future = loop.create_future()
    app = Host()
    async with app.run_test(size=(100, 44)) as pilot:
        box = app.query_one(ChatInput)
        card = cards["approval"](future)
        await app.mount(card, before=box)
        card._laid_out = True
        card._show_keys(False)  # before any layout: nothing measured
        assert card.query_one(card.KEYS_ID, Static).styles.min_height.value == 3
        await pilot.pause(0.2)
        box.text = "/he"
        await pilot.press("tab")
        await pilot.pause(0.1)
        assert app.focused is box, "tab left a slash command for the card"
    print("two cards: tab goes round, each waiting line names the key that leads there, esc is the last card's  ✓")


async def an_answered_card_gives_the_keyboard_back():
    """Real flows, with a turn's approval and the user's /proposals card on screen together. When
    one closes, the keyboard goes back to the message box. Left to itself, Textual hands it to the
    card above — a card that waits, and that the user never came to."""
    from rockycode.dream.proposals import Proposal, ProposalStore
    from rockycode.session import get_project

    def seed():
        ProposalStore().save(Proposal(name="card-keys-probe", description="a draft", project_id=get_project(Path.cwd()).id,
                                      project_name="cardkeys", evidence=["rk_x"], body="# card-keys-probe\n- a step"))

    # The user's card is above, the turn asks below it and is answered.
    seed()
    gate = asyncio.Event()
    app, engine, ran = build(["ls -la"], gate)
    async with app.run_test(size=(100, 48)) as pilot:
        await submit(pilot, app)
        box = app.query_one(ChatInput)
        for key in "/proposals":
            await pilot.press(key)
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: len(app.query(ProposalCard)) == 1)
        proposal = app.query_one(ProposalCard)
        assert await wait_until(pilot, lambda: app.focused is proposal), "the user asked for it: it has the keyboard"
        gate.set()
        assert await wait_until(pilot, lambda: card_of(app) is not None)
        approval = card_of(app)
        await pilot.pause(0.3)
        assert app.focused is proposal and BY_TAB in keys_line(approval) and "↵" not in keys_line(approval)
        await pilot.press("tab")  # the proposal card is the top one: Tab goes to the box …
        assert await wait_until(pilot, lambda: app.focused is box, timeout=2.0)
        await pilot.press("tab")  # … and on to the newest card
        assert await wait_until(pilot, lambda: app.focused is approval, timeout=2.0)
        await pilot.pause(REST)
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: ran == ["ls -la"])
        assert await wait_until(pilot, lambda: card_of(app) is None)
        await pilot.pause(0.3)
        assert app.focused is box and not proposal.has_focus, "the keyboard went on to the card above"
        assert not proposal._future.done() and BY_ENTER in keys_line(proposal)
        await come(pilot, app, proposal)
        await pilot.press("escape")  # later: the proposal stays pending
        assert await wait_until(pilot, lambda: len(app.query(ProposalCard)) == 0)
    assert_history_api_valid(engine.history)
    # The turn's approval is above and waits; the user opens /proposals under it and closes it.
    app, engine, ran = build(["ls -la"])
    async with app.run_test(size=(100, 48)) as pilot:
        await submit(pilot, app)
        approval, box = await a_card_opens(pilot, app)
        for key in "/proposals":
            await pilot.press(key)
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: len(app.query(ProposalCard)) == 1)
        proposal = app.query_one(ProposalCard)
        assert await wait_until(pilot, lambda: app.focused is proposal)
        await pilot.pause(REST)
        await pilot.press("escape")  # later
        assert await wait_until(pilot, lambda: len(app.query(ProposalCard)) == 0)
        await pilot.pause(0.3)
        assert app.focused is box and not approval.has_focus and not approval._future.done()
        assert cardkeys.last_on(app) is None, "the approval got the keyboard for a moment: it is now 'the card the user was on'"
        await pilot.press("x")
        await pilot.press("backspace")
        await pilot.pause()
        assert not approval._future.done() and BY_ENTER in keys_line(approval)
        await come(pilot, app, approval)
        await pilot.pause(REST)
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: ran == ["ls -la"])
    assert_history_api_valid(engine.history)
    print("an approval and the user's own card together: whichever closes, the keyboard goes back to the message box  ✓")


async def main():
    await never_takes_the_keyboard()
    await typed_on_the_card()
    await card_while_typing("interrupt")
    await card_while_typing("queue")
    await arrows_are_never_held_back()
    await esc_is_the_cards()
    await warned_call()
    await no_letter_answers()
    await rows_click_and_a_draft_is_safe()
    await another_card_has_the_keyboard()
    await the_wait_is_in_the_action()
    await keys_that_write_without_a_character()
    await the_last_line_says_who_has_the_keyboard()
    await two_cards()
    await an_answered_card_gives_the_keyboard_back()
    print("CARD KEYS SMOKE OK — typing is message, card not eat. amaze!")


asyncio.run(main())
