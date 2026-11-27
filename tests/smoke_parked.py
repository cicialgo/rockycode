"""yolo never waits — the approval pipeline's side of it (no TUI, no model, no network).

A few calls are the user's to decide every time (permission.human_only): a change
to rocky's settings, a shell command that names its key or settings files or reads
the OS keychain, `lake … --inadvisably-no-sandbox`. Asked in yolo, they put a card
in front of a run nobody was watching. With `park=True` (a host that shows a
waiting list):

  - yolo refuses the call at once, with words for the model, and keeps it for the user;
  - nothing is asked and the decision model is not consulted;
  - the user's "run it now" allows nothing by itself: the host arms a pass for that
    exact call for the one turn that picks it up, and disarms it after;
  - the third parked step in a turn ends the turn, in the engine: the rest of the
    batch is answered without running, and the turn finishes with its usage;
  - a block-tier command stays blocked, pass or no pass.
ask and careful, hosts without a waiting list, and exec are unchanged — also for a
malformed call whose command is not text.
"""
import asyncio
import json
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["ROCKYCODE_HOME"] = str(Path(tempfile.mkdtemp(prefix="rocky-parked-")) / ".rockycode")

sys.path.insert(0, str(ROOT / "tests"))

from rockycode.engine.approval import (  # noqa: E402
    PARK_LIMIT, PARKED_MAX, HeadlessApprover, Refusal, ToolApprover, tool_detail,
)
from rockycode.engine.events import ToolFinished, TurnFinished  # noqa: E402
from rockycode.engine.loop import Engine  # noqa: E402
from rockycode.engine.tools import Tool  # noqa: E402
from invariants import assert_history_api_valid, completed_chunks  # noqa: E402

workdir = Path(tempfile.mkdtemp(prefix="rocky-parked-repo-"))
registry = {name: Tool(name=name, schema={}, fn=None, risk="risky") for name in ("bash", "rocky_config")}
KEYS = {"command": "cat ~/.rockycode/.env"}
SETTING = {"action": "set", "key": "model", "value": "kimi:kimi-k3"}
asked, notes = [], []


async def ask(req):
    asked.append(req)
    return "deny"


async def notify(kind, **info):
    notes.append((kind, info))


class NoVerifier:
    """The decision model is never asked about a call only the user decides."""

    async def on_ask(self, **kwargs):
        raise AssertionError(f"the verifier was consulted: {kwargs}")

    async def on_allowed(self, **kwargs):
        return None


def approver(mode="yolo", park=True, verifier=False):
    asked.clear()
    notes.clear()
    return ToolApprover(registry=registry, workdir=workdir, ask=ask, mode=mode, notify=notify,
                        verifier=verifier, park=park)


def run(coro):
    return asyncio.run(coro)


# ── yolo: refused at once, kept for the user, nobody asked ───────────────────
a = approver(verifier=NoVerifier())
answer = run(a("bash", KEYS))
assert isinstance(answer, Refusal) and not answer, "a refusal is falsy: every host that reads a bool still denies"
assert answer.startswith("[parked]") and "another way" in answer and "key file" in answer, answer
assert not asked, "yolo does not ask"
assert [(p.tool, p.detail, p.why) for p in a.parked] == [("bash", KEYS["command"], "rocky-store")]
assert a.parked[0].warning and notes[-1][0] == "parked" and notes[-1][1]["waiting"] == 1
answer = run(a("rocky_config", SETTING))
assert isinstance(answer, Refusal) and "changes rocky's own settings" in answer
assert a.parked[1].detail.endswith("model = kimi:kimi-k3"), "the card will show the change in plain words"
assert run(a("rocky_config", {"action": "show"})) is True and run(a("bash", {"command": "ls -la"})) is True
assert len(a.parked) == 2 and not asked, "ordinary yolo is untouched"
for command, why in (("lake check --inadvisably-no-sandbox", "lake-no-sandbox"),
                     ("security find-generic-password -s rockycode -w", "keychain-read")):
    b = approver()
    assert isinstance(run(b("bash", {"command": command})), Refusal) and b.parked[0].why == why and not asked
assert Engine._refused(answer) == str(answer) and Engine._refused(False).startswith("[denied]"), \
    "the model reads the refusal's own words, not the stock line"
print("yolo: a step only the user can allow is refused at once and kept; nothing asked  ✓")

# ── unchanged: ask and careful ask, a host without a waiting list asks, exec stops ──
for mode in ("ask", "careful"):
    a = approver(mode=mode)
    assert run(a("bash", KEYS)) is False and len(asked) == 1 and not a.parked, mode
    assert asked[0].session_grant is None and asked[0].warning
a = approver(park=False)
assert run(a("bash", KEYS)) is False and len(asked) == 1 and not a.parked, "serve and the desktop keep their card"
headless = HeadlessApprover({}, frozenset())
assert run(headless("bash", KEYS)) is False and headless.blocked["grant"] == "rocky-store" and not headless.parked
# A malformed call (the command is a list) is judged like any other: no host trips over the pass check.
odd = {"command": ["ls", "-la"]}
assert tool_detail("bash", odd) == "['ls', '-la']" and isinstance(tool_detail("web_fetch", {"url": 7}), str)
a = approver(mode="ask", park=False)
assert run(a("bash", odd)) is False and len(asked) == 1 and isinstance(asked[0].detail, str)
assert run(HeadlessApprover({}, frozenset())("bash", odd)) in (True, False)
assert isinstance(run(approver()("bash", {"command": ["cat", "~/.rockycode/.env"]})), Refusal)
print("ask, careful, hosts without a waiting list and exec: as before, a malformed call too  ✓")

# ── the user's answer: that exact call, one time, in the turn that picks it up ──
a = approver()
run(a("bash", KEYS))
step = a.parked[0]
a.settle(step)
assert not a.parked and not a.once, "deciding takes it off the list and allows nothing by itself"
assert isinstance(run(a("bash", KEYS)), Refusal), "no pick-up turn, no pass: the call parks again"
a.settle(a.parked[0])
a.arm([step])  # the host: the turn that delivers "run it now" begins
assert a.once == {step.key}
assert run(a("bash", {"command": KEYS["command"] + " | base64"})) is not True, "a changed command is a new step"
assert [p.detail for p in a.parked] == [KEYS["command"] + " | base64"] and a.once == {step.key}
assert run(a("bash", KEYS)) is True and not a.once, "the allowed call runs"
assert isinstance(run(a("bash", KEYS)), Refusal), "once: the same call parks again"
other = a.parked[0]
a.arm([step, other])
a.disarm([step])  # that turn is over: its unused pass ends, another turn's pass does not
assert a.once == {other.key}
a.disarm([other])
assert not a.once and isinstance(run(a("bash", KEYS)), Refusal)
print("run it now: armed for its own turn, exact, used once, gone after; deciding alone allows nothing  ✓")

# ── the limit: the third parked step in a turn stops the turn ────────────────
a = approver()
a.begin_turn()
for n in range(1, PARK_LIMIT + 1):
    answer = run(a("bash", KEYS))  # the same step again and again still counts
    assert a.stop_turn is (n == PARK_LIMIT), n
assert len(a.parked) == 1, "listed once"
assert "third such step" in answer and "stops the turn" in answer and notes[-1][1]["stop"] is True
a.begin_turn()
assert not a.stop_turn and a.turn_parks == 0, "the limit counts per turn"
for n in range(PARKED_MAX + 5):
    a.begin_turn()
    answer = run(a("bash", {"command": f"cat ~/.rockycode/.env  # {n}"}))
    assert isinstance(answer, Refusal)
assert len(a.parked) == PARKED_MAX and notes[-1][1]["listed"] is False, "the list is for a person: it has an end"
assert answer.startswith("[denied]") and "waiting list is full" in answer and "waits for the user" not in answer, \
    "a step that is not kept is not said to wait"


# The engine ends the turn itself: one message with five calls, the third parks at the limit.
class Usage:
    def model_dump(self):
        return {"prompt_tokens": 30, "completion_tokens": 6}


def one_message(commands):
    calls = [types.SimpleNamespace(index=i, id=f"c{i}", function=types.SimpleNamespace(
        name="bash", arguments=json.dumps({"command": c}))) for i, c in enumerate(commands)]
    delta = types.SimpleNamespace(reasoning_content=None, content=None, tool_calls=calls)
    return [types.SimpleNamespace(usage=None, choices=[types.SimpleNamespace(delta=delta)]),
            types.SimpleNamespace(usage=Usage(), choices=[types.SimpleNamespace(
                delta=types.SimpleNamespace(reasoning_content=None, content=None, tool_calls=None))])]


requests, ran = [], []


async def create(**kwargs):
    requests.append(kwargs)
    assert len(requests) == 1, "the model is not asked again after the stop"

    async def stream():
        for part in completed_chunks(one_message([f"cat ~/.rockycode/{n}" for n in (".env", "config.toml", "models.toml")]
                                                 + ["echo four", "echo five"])):
            yield part
    return stream()


async def bash(command):
    ran.append(command)
    return "ok"


async def stopped_turn():
    engine = Engine(model="fake", client=types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))),
                    workdir=workdir, registry={"bash": Tool(name="bash", schema={"type": "function", "function": {
                        "name": "bash", "parameters": {"type": "object", "properties": {}}}}, fn=bash, risk="risky")})
    engine.approver = ToolApprover(registry=engine.registry, workdir=workdir, ask=ask, mode="yolo", verifier=False,
                                   park=True, engine=engine)
    engine.approver.turn_parks = 2  # a stale count from the turn before: the engine starts each turn afresh
    events = [event async for event in engine.run_turn("look around")]
    return engine, events


engine, events = run(stopped_turn())
outputs = [e.output for e in events if isinstance(e, ToolFinished)]
assert [o.split("]")[0] for o in outputs] == ["[parked"] * 3 + ["[not run"] * 2, outputs
assert ran == [] and engine.approver.stop_turn and len(engine.approver.parked) == 3
finished = [e for e in events if isinstance(e, TurnFinished)]
assert len(finished) == 1 and finished[0].usage.get("prompt_tokens") == 30, "the turn finishes, usage and all"
assert engine.stats.denials == 0, "none of this is the user saying no"
assert_history_api_valid(engine.history)
print(f"limit: step {PARK_LIMIT} in one turn ends it mid-batch, usage counted; the list holds {PARKED_MAX} and says when it is full  ✓")

# ── block-tier stays blocked; a loop tick parks too ───────────────────────────
a = approver()
wipe = {"command": "sudo rm -rf /"}
a.once.add(("bash", wipe["command"]))
assert run(a("bash", wipe)) is False and a.refused and not a.parked, "a pass never unblocks a blocked command"
a = approver()
a.tick.loop = object()
assert isinstance(run(a("bash", KEYS)), Refusal) and len(a.parked) == 1 and a.tick.blocked is None and not asked, \
    "a yolo tick parks the step for the user instead of dropping it"
print("block-tier is still refused; a loop tick in yolo parks for the user  ✓")

print("PARKED SMOKE OK — yolo not wait, human still decide. amaze!")
