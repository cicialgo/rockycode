"""The /loop core (engine/cron.py) — pure, no API, no Textual.

Contracts under test:
  - the argument line: interval first, budget clauses only while they parse,
    the rest is the prompt; the 1m floor; clear errors.
  - the clock face for a wall-clock time (half-hour resolution).
  - the tick marker is byte-stable apart from its header; the reply parser
    takes the LAST marker, reads the dash message, and treats a missing
    marker as NOTE (never silent).
  - the book: ids, due-once (missed ticks never stack), next due counted from
    the END of a tick, quiet streaks, pause/resume, resolve with an optional
    id.
  - budgets: NOTHING ends a loop the user did not set; for / x / max end it
    with the reason; LOOP DONE ends it as done.
  - reminders: hour marks and spend marks, at most one an hour, none for a
    paused loop, none of a kind the user already capped.
  - the tools: loop_start creates (with budgets only when passed), loop_stop
    stops, both refuse inside a tick, bad input is a readable [error].
  - Engine.rollback: drops the tail, clamps the cursor, keeps history valid
    and the trajectory complete.
"""
import asyncio
import json
import os
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.environ["ROCKYCODE_HOME"] = tempfile.mkdtemp(prefix="rockyhome-")
os.chdir(tempfile.mkdtemp(prefix="rockycron-"))

from rockycode.engine import cron as C
from rockycode.engine.tools import execute
from invariants import assert_history_api_valid


def test_parse_args():
    spec, err = C.parse_loop_args("5m check the bench")
    assert not err and spec["interval_s"] == 300 and spec["prompt"] == "check the bench", (spec, err)
    assert spec["for_s"] is None and spec["count"] is None and spec["max_spend"] is None

    spec, err = C.parse_loop_args("10m for 3h x12 max $2 do the next step")
    assert not err, err
    assert spec["interval_s"] == 600 and spec["for_s"] == 3 * 3600 and spec["count"] == 12
    assert spec["max_spend"] == 2.0 and spec["prompt"] == "do the next step", spec

    spec, err = C.parse_loop_args("1h max ¥5 x3 check")  # any clause order, yuan sign
    assert not err and spec["max_spend"] == 5.0 and spec["count"] == 3 and spec["prompt"] == "check", (spec, err)

    spec, err = C.parse_loop_args("5m for the record, check the log")  # `for` + non-duration = prompt
    assert not err and spec["for_s"] is None and spec["prompt"] == "for the record, check the log", (spec, err)

    spec, err = C.parse_loop_args("5min ping")
    assert not err and spec["interval_s"] == 300, (spec, err)

    for bad, needle in [("5s check", "shortest interval"), ("soon check", "not an interval"),
                        ("5m", "what should each tick do"), ("", "usage"),
                        ("5m x0 check", "positive count"), ("5m max $0 check", "positive amount")]:
        spec, err = C.parse_loop_args(bad)
        assert spec is None and needle in err, (bad, spec, err)

    assert C.fmt_duration(300) == "5m" and C.fmt_duration(3600) == "1h"
    assert C.fmt_duration(5400) == "1h30m" and C.fmt_duration(90) == "90s" and C.fmt_duration(86400) == "1d"
    print("  args ok")


def test_clock():
    def face(h, m):
        return C.clock_face(datetime(2026, 9, 30, h, m))
    assert face(14, 35) == "\U0001F55D", hex(ord(face(14, 35)))  # 🕝 two-thirty
    assert face(14, 50) == "\U0001F552"                          # 🕒 three
    assert face(0, 10) == "\U0001F55B"                           # 🕛 twelve
    assert face(12, 0) == "\U0001F55B"
    assert face(6, 44) == "\U0001F561"                           # 🕡 six-thirty
    assert face(23, 50) == "\U0001F55B"                          # wraps to twelve
    assert face(1, 0) == "\U0001F550"                            # 🕐 one
    print("  clock ok")


def test_marker_and_reply():
    book = C.LoopBook()
    lp = book.add("check results/", 300, now=1000.0)
    at = datetime(2026, 9, 30, 14, 35)
    m1 = C.tick_marker(lp, 3, at)
    m2 = C.tick_marker(lp, 3, at)
    assert m1 == m2 and m1.startswith("[loop #1 · tick 3 · every 5m · 14:35] check results/\n\n")
    assert m1.endswith(C.TICK_RULES)
    for word in ("LOOP QUIET", "LOOP NOTE", "LOOP DONE"):
        assert word in m1
    # only the header varies between ticks — the rules are byte-stable
    m3 = C.tick_marker(lp, 4, datetime(2026, 9, 30, 14, 40))
    assert m3.split("]", 1)[1] == m1.split("]", 1)[1]

    assert C.parse_tick_reply("nothing new in results/.\nLOOP QUIET") == (C.QUIET, "")
    assert C.parse_tick_reply("**LOOP NOTE** — tests failed on py311") == (C.NOTE, "tests failed on py311")
    assert C.parse_tick_reply("all steps checked.\nloop done - every step is ticked") == (C.DONE, "every step is ticked")
    assert C.parse_tick_reply("I looked, still running.") == (C.NOTE, "")   # missing → visible
    assert C.parse_tick_reply("") == (C.NOTE, "")
    # the LAST marker wins — quoting the rules earlier must not decide
    quoted = "The rules say LOOP DONE when finished. Not yet.\nLOOP QUIET — still running"
    assert C.parse_tick_reply(quoted) == (C.QUIET, "still running")
    assert C.parse_tick_reply("LOOP DONE — run 3 finished\nsee summary.json\n") == (C.DONE, "run 3 finished")

    est = C.tick_cost_estimate(180_000, {"in_hit": 0.028, "in_miss": 0.28, "out": 0.42})
    assert 0.004 < est < 0.006, est
    print("  marker + reply ok")


def test_book():
    book = C.LoopBook()
    a = book.add("a", 300, now=1000.0)
    b = book.add("b", 600, now=1000.0)
    assert (a.id, b.id) == (1, 2) and a.next_due == 1300.0 and a.live
    assert book.due(now=1299.0) == []
    assert book.due(now=1300.0) == [a]
    assert [lp.id for lp in book.due(now=5000.0)] == [1, 2]          # oldest first, each ONCE
    assert book.next_due(now=1000.0) is a

    n = book.begin_tick(a, now=5000.0)
    assert n == 1 and a.last_tick == 5000.0 and book.tick_active == 1
    assert book.due(now=9000.0) == [], "nothing is due while a tick runs"
    assert book.end_tick(a, C.QUIET, cost=0.01, now=5040.0) is None
    assert book.tick_active is None and a.spend == 0.01 and a.quiet_streak == 1
    assert a.next_due == 5340.0, "interval counts from the END of the tick"
    assert book.due(now=5339.0) == [b] and a in book.due(now=5340.0)
    book.begin_tick(a, now=5340.0)
    book.end_tick(a, C.NOTE, message="found it", now=5350.0)
    assert a.quiet_streak == 0 and a.last_verdict == C.NOTE and a.last_message == "found it"

    # pause: not due; resume: due NOW, not a whole interval later
    book.pause(a, blocked_on={"tool": "bash", "detail": "pip install x"})
    assert a.status == "paused" and a not in book.due(now=99999.0) and a.blocked_on["tool"] == "bash"
    book.resume(a, now=6000.0)
    assert a.status == "running" and a.blocked_on is None and a in book.due(now=6000.0)
    # resume_blocked (a switch to yolo): approval pauses come back, a manual
    # /loop pause is the user's own and stays — on its own book so the counts
    # below stay what they were
    bb = C.LoopBook()
    held = bb.add("install it", 300.0, now=0.0)
    parked = bb.add("hold this", 300.0, now=0.0)
    bb.pause(held, blocked_on={"tool": "bash", "detail": "pip install x"})
    bb.pause(parked)
    back = bb.resume_blocked(now=7000.0)
    assert back == [held] and held.status == "running" and held in bb.due(now=7000.0)
    assert parked.status == "paused" and parked.blocked_on is None
    assert bb.resume_blocked(now=7001.0) == [], "nothing left to resume"

    # resolve: optional id when exactly one loop is live
    lp, err = book.resolve(None)
    assert lp is None and "which one" in err
    lp, err = book.resolve("#2")
    assert lp is b and not err
    lp, err = book.resolve("9")
    assert lp is None and "no live loop" in err
    lp, err = book.resolve("two")
    assert lp is None and "not a loop id" in err
    book.stop(b, "stopped")
    assert b.status == "stopped" and b.end_reason == "stopped" and not b.live
    lp, err = book.resolve(None)
    assert lp is a and not err
    lp, err = book.resolve("2")
    assert lp is None and "no live loop" in err
    book.stop(a)
    lp, err = book.resolve(None)
    assert lp is None and "no loops running" in err
    assert book.live() == [] and len(book.all()) == 2
    print("  book ok")


def test_budgets():
    # no budget: a thousand ticks, hours of wall-clock, real spend — never ends
    book = C.LoopBook()
    lp = book.add("free", 60, now=0.0)
    for i in range(1000):
        book.begin_tick(lp, now=i * 3600.0)
        assert book.end_tick(lp, C.QUIET, cost=0.9, now=i * 3600.0 + 30) is None
    assert lp.status == "running" and lp.ticks == 1000 and lp.spend > 800

    lp = book.add("count", 60, count=3, now=0.0)
    for i in range(2):
        book.begin_tick(lp, now=i * 100.0)
        assert book.end_tick(lp, C.NOTE, now=i * 100.0 + 5) is None
    book.begin_tick(lp, now=300.0)
    assert book.end_tick(lp, C.NOTE, now=305.0) == "x3 ticks reached" and lp.status == "stopped"

    lp = book.add("dur", 60, for_s=3600, now=0.0)
    book.begin_tick(lp, now=3000.0)
    assert book.end_tick(lp, C.QUIET, now=3010.0) is None
    book.begin_tick(lp, now=3590.0)
    assert book.end_tick(lp, C.QUIET, now=3600.0) == "1h reached"

    lp = book.add("spend", 60, max_spend=1.0, now=0.0)
    book.begin_tick(lp, now=10.0)
    assert book.end_tick(lp, C.QUIET, cost=0.6, now=20.0) is None
    book.begin_tick(lp, now=100.0)
    assert book.end_tick(lp, C.QUIET, cost=0.5, now=110.0) == "max $1 reached"
    assert lp.budget_note("$") == "max $1"

    cn = C.LoopBook(currency="cny")
    lp = cn.add("cny", 60, max_spend=7, for_s=7200, count=9, now=0.0)
    assert lp.budget_note(cn.sym) == "for 2h · x9 · max ¥7"

    lp = book.add("done", 60, now=0.0)
    book.begin_tick(lp, now=10.0)
    assert book.end_tick(lp, C.DONE, message="run 3 finished", now=20.0) == "run 3 finished"
    assert lp.status == "done" and lp.end_reason == "run 3 finished"

    # stopped mid-tick (user /loop stop while it ran): end_tick settles, never revives
    lp = book.add("mid", 60, now=0.0)
    book.begin_tick(lp, now=10.0)
    book.stop(lp, "stopped")
    assert book.end_tick(lp, C.NOTE, now=20.0) is None and lp.status == "stopped"
    assert book.tick_active is None
    print("  budgets ok")


def test_reminders():
    book = C.LoopBook()
    lp = book.add("r", 300, now=0.0)
    assert book.reminder_due(lp, now=1800.0) is None                      # 30 min: nothing
    r = book.reminder_due(lp, now=3600.0)
    assert r and r["hours"] == 1.0 and r["ticks"] == 0, r                 # 1h mark
    assert book.reminder_due(lp, now=5400.0) is None                       # 1h30: gap not elapsed
    assert book.reminder_due(lp, now=7200.0) is not None                   # 2h mark
    assert book.reminder_due(lp, now=10800.0) is None                      # 3h: no mark (next is 4h)
    assert book.reminder_due(lp, now=14400.0) is not None                  # 4h
    # spend mark, respecting the hourly gap
    lp.spend = 0.55
    assert book.reminder_due(lp, now=14400.0 + 600) is None                # 10 min after the last
    r = book.reminder_due(lp, now=14400.0 + 3600)
    assert r and abs(r["spend"] - 0.55) < 1e-9
    lp.spend = 0.80
    assert book.reminder_due(lp, now=14400.0 + 7200) is None               # 0.80 < next 1.00 mark, no hour mark
    lp.spend = 1.00
    assert book.reminder_due(lp, now=14400.0 + 7200) is not None
    # paused → silent
    book.pause(lp)
    lp.spend = 9.0
    assert book.reminder_due(lp, now=99999.0) is None
    # a loop the user capped by time gets no hour marks (spend marks still)
    capped = book.add("c", 300, for_s=8 * 3600, now=0.0)
    assert book.reminder_due(capped, now=3600.0) is None
    capped.spend = 0.5
    assert book.reminder_due(capped, now=3600.0) is not None
    # cny steps at ¥5
    cn = C.LoopBook(currency="cny")
    lp = cn.add("y", 300, now=0.0)
    lp.spend = 4.9
    assert cn.reminder_due(lp, now=100.0) is None
    lp.spend = 5.0
    assert cn.reminder_due(lp, now=100.0) is not None
    print("  reminders ok")


async def test_tools():
    book = C.LoopBook()
    events = []

    async def notify(kind, loop):
        events.append((kind, loop.id))

    reg = C.build_loop_tools(book)
    book.notify = notify
    assert reg["loop_start"].risk == "risky" and reg["loop_stop"].risk == "safe"

    out, ok = await execute(reg, "loop_start", json.dumps({"interval": "10m", "prompt": "check it"}))
    assert ok and out.startswith("loop #1 started — every 10m · no budget"), out
    lp = book.get(1)
    assert lp.origin == "model" and lp.for_s is None and events == [("start", 1)]

    out, ok = await execute(reg, "loop_start", json.dumps(
        {"interval": "1h", "prompt": "next step", "for": "3h", "count": 4, "max_spend": 2}))
    assert ok and "for 3h · x4 · max $2" in out, out
    lp2 = book.get(2)
    assert lp2.for_s == 10800 and lp2.count == 4 and lp2.max_spend == 2.0

    for args, needle in [({"interval": "5s", "prompt": "x"}, "shortest interval"),
                         ({"interval": "later", "prompt": "x"}, "not an interval"),
                         ({"interval": "5m", "prompt": "  "}, "empty"),
                         ({"interval": "5m", "prompt": "x", "for": "soon"}, "'for' must be"),
                         ({"interval": "5m", "prompt": "x", "count": 0}, "positive"),
                         ({"interval": "5m", "prompt": "x", "max_spend": -1}, "positive")]:
        out, ok = await execute(reg, "loop_start", json.dumps(args))
        assert not ok and needle in out, (args, out)
    assert len(book.all()) == 2, "bad input must not create loops"

    # loops don't breed, don't kill each other: refused inside a tick
    book.begin_tick(lp, now=10.0)
    out, ok = await execute(reg, "loop_start", json.dumps({"interval": "5m", "prompt": "child"}))
    assert not ok and "LOOP DONE" in out and len(book.all()) == 2
    out, ok = await execute(reg, "loop_stop", json.dumps({"id": 2}))
    assert not ok and lp2.status == "running"
    book.end_tick(lp, C.QUIET, now=20.0)

    out, ok = await execute(reg, "loop_stop", json.dumps({"id": 2}))
    assert ok and out.startswith("loop #2 stopped") and lp2.status == "stopped" and events[-1] == ("stop", 2)
    out, ok = await execute(reg, "loop_stop", json.dumps({"id": 2}))
    assert not ok and "no live loop #2" in out
    out, ok = await execute(reg, "loop_stop", json.dumps({"id": "abc"}))
    assert not ok and "integer" in out
    print("  tools ok")


# ---- Engine.rollback: fake DeepSeek stream, same seams as smoke_engine ------

class _U:
    def model_dump(self):
        return {"prompt_tokens": 100, "completion_tokens": 10}


def _chunk(content=None, tool_calls=None, usage=None):
    if usage is not None and content is None and tool_calls is None:
        return types.SimpleNamespace(usage=usage, choices=[])
    d = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(usage=usage, choices=[types.SimpleNamespace(delta=d)])


def _tc(i, id_, name, args):
    return types.SimpleNamespace(index=i, id=id_, function=types.SimpleNamespace(name=name, arguments=args))


async def _stream(chunks):
    for c in chunks:
        yield c


class _FC:
    """turn 1: plain answer · turn 2: one tool call then an answer."""
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls == 2:
            return _stream([_chunk(tool_calls=[_tc(0, "c1", "bash", json.dumps({"command": "ls"}))]),
                            _chunk(usage=_U())])
        return _stream([_chunk(content="ok"), _chunk(usage=_U())])


async def test_rollback():
    from rockycode.engine.loop import Engine
    from rockycode.engine.tools import Tool

    async def bash(command):
        return "files"

    reg = {"bash": Tool(name="bash", schema={"type": "function", "function": {"name": "bash", "parameters": {"type": "object", "properties": {}}}}, fn=bash, risk="risky")}
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=_FC()))
    eng = Engine(model="fake", client=client, workdir=Path.cwd(), registry=reg)
    async for _ in eng.run_turn("hi"):
        pass
    n = len(eng.history)                      # system + user + assistant = 3
    assert n == 3, eng.history
    async for _ in eng.run_turn("[loop #1 · tick 1] check"):
        pass
    assert len(eng.history) == 3 + 4, [m["role"] for m in eng.history]   # user, assistant(tool), tool, assistant
    assert eng._sent_until > n
    assert eng.rollback(n, reason="loop quiet") == 4
    assert len(eng.history) == n and eng._sent_until <= n
    assert_history_api_valid(eng.history)
    assert eng.rollback(n) == 0 and eng.rollback(0) == 0 and eng.rollback(99) == 0   # no-ops
    # trajectory: every message still there + the rollback note (append-only)
    recs = [json.loads(l) for l in Path(eng.trajectory.path).read_text(encoding="utf-8").splitlines()]
    msgs = [r for r in recs if r["kind"] == "message"]
    assert len(msgs) == 1 + 2 + 4, len(msgs)  # system + turn 1 + turn 2
    notes = [r["data"] for r in recs if r["kind"] == "note" and "rollback" in r["data"]]
    assert notes and notes[-1]["rollback"] == {"to": 3, "dropped": 4, "reason": "loop quiet"}
    # the engine still works after a rollback (a 3rd turn appends cleanly)
    async for _ in eng.run_turn("again"):
        pass
    assert_history_api_valid(eng.history)
    assert len(eng.history) == n + 2
    print("  rollback ok")


test_parse_args()
test_clock()
test_marker_and_reply()
test_book()
test_budgets()
test_reminders()
asyncio.run(test_tools())
asyncio.run(test_rollback())
print("CRON CORE SMOKE OK — args · clock · marker/reply · book · budgets-only-when-set · reminders · tools · rollback. amaze!")
