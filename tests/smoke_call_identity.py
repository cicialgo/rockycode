"""Call identity, phase 1 (docs/design/call-identity.md): every tool call carries one
logical id and leaves one receipt — serial, parallel, bounded continuation, history fork
and an SDK retry — and the catalog generation is stamped on each. No API calls."""
import asyncio
import copy
import json
import os
import tempfile
import types
from pathlib import Path

import httpx
from openai import AsyncOpenAI

home = Path(tempfile.mkdtemp(prefix="rocky-identity-"))
os.environ["ROCKYCODE_HOME"] = str(home)
os.chdir(home)

from invariants import completed_chunks
from rockycode import headless
from rockycode.engine import explore, loop as loop_mod
from rockycode.engine.events import (
    CURRENT_CALL, EngineError, Envelope, ToolFinished, ToolStarted, TurnFinished, TurnStarted,
)
from rockycode.engine.loop import Engine
from rockycode.engine.tools import Tool
from rockycode.runtime import Runtime
from rockycode.server import _event_to_notification


class Usage:
    def model_dump(self):
        return {"prompt_tokens": 10, "completion_tokens": 2}


def chunk(content=None, tool_calls=None, usage=None):
    if usage is not None:
        return types.SimpleNamespace(usage=usage, choices=[])
    delta = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(usage=None, choices=[types.SimpleNamespace(delta=delta)])


def tc(index, id_, name, args="{}"):
    return types.SimpleNamespace(index=index, id=id_,
                                 function=types.SimpleNamespace(name=name, arguments=args))


def calls(*specs):
    return [chunk(tool_calls=[tc(i, id_, name) for i, (id_, name) in enumerate(specs)]), chunk(usage=Usage())]


def text(s):
    return [chunk(content=s), chunk(usage=Usage())]


async def stream_from(chunks):
    for c in completed_chunks(chunks):
        yield c


class Script:
    """A scripted chat.completions: each create() pops the next response; an
    exception entry is raised (a dead transport); before_request is the host's
    checkpoint hook, like exec's admission point."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.before_request = lambda: None
        self.requests = 0

    async def create(self, **kwargs):
        self.before_request()
        self.requests += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return stream_from(response)


def client(responses):
    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=Script(responses)))


def schema(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object", "properties": {}}}}


seen: list = []  # what each tool saw in CURRENT_CALL while it ran


async def read(path: str = "") -> str:
    seen.append(CURRENT_CALL.get())
    await asyncio.sleep(0)
    return f"read {path}"


async def write(path: str = "", content: str = "") -> str:
    seen.append(CURRENT_CALL.get())
    return "wrote"


async def fail() -> str:
    seen.append(CURRENT_CALL.get())
    return "[error] boom"


def registry():
    return {"read": Tool("read", schema("read"), read, risk="safe"),
            "write": Tool("write", schema("write"), write),
            "fail": Tool("fail", schema("fail"), fail, risk="safe")}


def engine(responses, **kw):
    return Engine(model="fake-model", client=client(responses), workdir=home, registry=registry(), **kw)


def notes(eng, key):
    lines = [json.loads(line) for line in eng.trajectory.path.read_text().splitlines()]
    return [line["data"][key] for line in lines if line["kind"] == "note" and key in line["data"]]


def pairs(events):
    """ToolStarted/ToolFinished matched by logical id: exactly one of each per call."""
    started = {e.envelope.logical_call_id: e for e in events if isinstance(e, ToolStarted)}
    finished = {e.envelope.logical_call_id: e for e in events if isinstance(e, ToolFinished)}
    assert len(started) == sum(isinstance(e, ToolStarted) for e in events), "logical ids repeat"
    assert started.keys() == finished.keys(), (started.keys(), finished.keys())
    for lid, s in started.items():
        f = finished[lid]
        assert s.envelope == f.envelope and s.call_id == f.call_id == s.envelope.provider_call_id, lid
    return started, finished


async def serial_parallel_refused():
    eng = engine([calls(("c1", "write"), ("c2", "read")),            # a write → serial
                  calls(("c1", "read"), ("c2", "read"), ("c3", "fail")),  # all safe → parallel, ids reused
                  text("done"),
                  calls(("c1", "read")), text("again"),
                  calls(("c1", "write")), text("refused")])
    turn1 = [e async for e in eng.run_turn("first")]
    turn2 = [e async for e in eng.run_turn("second")]

    async def deny(name, args):
        return name != "write"
    eng.approver = deny
    turn3 = [e async for e in eng.run_turn("third")]

    for events, n in ((turn1, 5), (turn2, 1), (turn3, 1)):
        started, finished = pairs(events)
        assert len(started) == n
        turn = next(e for e in events if isinstance(e, TurnStarted)).turn
        assert turn and turn == next(e for e in events if isinstance(e, TurnFinished)).turn
        for e in started.values():
            env = e.envelope
            assert env.turn == turn and env.session == eng.trajectory.session_id
            assert env.attempt == 1 and env.registry_generation == 0
            assert env.route == {"endpoint": eng.provider_name, "model": "fake-model",
                                 "protocol": eng.protocol.label}
    turns = [next(e for e in ev if isinstance(e, TurnStarted)).turn for ev in (turn1, turn2, turn3)]
    assert len(set(turns)) == 3, turns
    # Provider ids recur across requests and turns; logical ids never do.
    all_started = [e for ev in (turn1, turn2, turn3) for e in ev if isinstance(e, ToolStarted)]
    assert [e.call_id for e in all_started].count("c1") == 4
    assert len({e.envelope.logical_call_id for e in all_started}) == 7
    assert len({e.envelope.request for e in all_started}) == 4  # four responses carried calls
    # Each tool ran under its own envelope, concurrently too; nothing leaks outside.
    assert [s.logical_call_id for s in seen if s] == [e.envelope.logical_call_id for e in all_started
                                                      if e.tool != "write" or e in turn1]
    assert CURRENT_CALL.get() is None
    # One receipt per call, keyed by the logical id; the error and the refusal named.
    receipts = {c["logical_call_id"]: c for c in notes(eng, "call")}
    assert receipts.keys() == {e.envelope.logical_call_id for e in all_started}
    by_tool = {}
    for c in receipts.values():
        by_tool.setdefault(c["tool"], []).append(c["receipt"])
    assert by_tool == {"write": ["ok", "refused"], "read": ["ok"] * 4, "fail": ["error"]}, by_tool
    assert all(c["executed_generation"] == 0 and c["route"]["model"] == "fake-model" for c in receipts.values())
    refused = next(e for e in turn3 if isinstance(e, ToolFinished))
    assert not refused.ok and receipts[refused.envelope.logical_call_id]["receipt"] == "refused"
    # One request line per model request; a fake transport reports the one send it knows of.
    requests = notes(eng, "request")
    assert len(requests) == 7 and {r["attempts"] for r in requests} == {1}
    assert not any(r["attempts_observed"] for r in requests)
    assert [r["step"] for r in requests] == [1, 2, 3, 1, 2, 1, 2]
    assert {r["turn"] for r in requests} == set(turns)
    print("serial + parallel + refused: one logical id and one receipt per call; provider ids recur ✓")


async def bounded_continuation():
    first = engine([calls(("c1", "read")), calls(("c2", "read")), RuntimeError("transport down")],
                   max_steps=6, finalize_steps=2)
    checkpoints = []
    script = first.client.chat.completions

    def checkpoint():
        state = first.export_turn_state()
        if state is not None:
            checkpoints.append((copy.deepcopy(first.history), state))
    script.before_request = checkpoint
    events = [e async for e in first.run_turn("investigate")]
    assert any(isinstance(e, EngineError) for e in events)
    turn = next(e for e in events if isinstance(e, TurnStarted)).turn
    history, state = checkpoints[-1]
    assert state["step"] == 3 and state["turn"] == turn, state
    # The failed request still left a request line (attempts, error), no call receipts.
    failed = notes(first, "request")[-1]
    assert failed["error"] == "RuntimeError" and failed["attempts"] == 1 and failed["turn"] == turn

    second = engine([calls(("c3", "read")), text("done")], max_steps=6, finalize_steps=2)
    second.history = copy.deepcopy(history)
    resumed = [e async for e in second.resume_turn(state=state)]
    started, _ = pairs(resumed)
    env = next(iter(started.values())).envelope
    assert env.turn == turn and env.session == second.trajectory.session_id != first.trajectory.session_id
    assert next(e for e in resumed if isinstance(e, TurnFinished)).turn == turn
    earlier = {e.envelope.logical_call_id for e in events if isinstance(e, ToolStarted)}
    assert earlier.isdisjoint(started) and len(earlier) == 2
    bad = copy.deepcopy(state)
    bad["turn"] = 7
    try:
        async for _ in second.resume_turn(state=bad):
            pass
    except ValueError:
        pass
    else:
        raise AssertionError("a non-string turn id was accepted")
    print("bounded continuation keeps the turn id across engines; new session, new logical ids ✓")


async def history_fork():
    a = engine([calls(("c1", "read")), text("a done")])
    a_events = [e async for e in a.run_turn("look")]
    b = engine([calls(("c1", "read")), text("b done")])
    b.resume(a.history, from_session=a.trajectory.session_id)
    b_events = [e async for e in b.run_turn("look again")]
    a_started, _ = pairs(a_events)
    b_started, _ = pairs(b_events)
    (a_call,), (b_call,) = a_started.values(), b_started.values()
    assert a_call.call_id == b_call.call_id == "c1"
    assert a_call.envelope.logical_call_id != b_call.envelope.logical_call_id
    assert a_call.envelope.session == a.trajectory.session_id
    assert b_call.envelope.session == b.trajectory.session_id != a.trajectory.session_id
    assert a_call.envelope.turn != b_call.envelope.turn
    assert notes(b, "resumed_from") == [a.trajectory.session_id]
    assert len(notes(b, "call")) == 1 and notes(b, "call")[0]["session"] == b.trajectory.session_id
    print("history fork: a new session that records its origin; no id collides with the parent's ✓")


def sse(chunks):
    return "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"


def wire(delta, finish=None, usage=None):
    out = {"id": "fixture", "object": "chat.completion.chunk", "created": 0, "model": "fake-model",
           "choices": [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage:
        out["usage"] = usage
    return out


USAGE = {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}
TOOL = [wire({"role": "assistant", "tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                                     "function": {"name": "read", "arguments": "{}"}}]}),
        wire({}, finish="tool_calls"), wire(None, usage=USAGE)]
TEXT = [wire({"role": "assistant", "content": "done"}), wire({}, finish="stop"), wire(None, usage=USAGE)]


async def sdk_retry():
    sends = []

    def respond(request):
        sends.append(request.headers.get("x-stainless-retry-count"))
        if len(sends) == 1:
            return httpx.Response(500, json={"error": {"message": "flaky"}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text=sse(TOOL if len(sends) == 2 else TEXT))

    async with AsyncOpenAI(api_key="fixture", base_url="https://fixture.invalid/v1", max_retries=2,
                           http_client=httpx.AsyncClient(trust_env=False,
                                                         transport=httpx.MockTransport(respond))) as sdk:
        eng = Engine(model="fake-model", client=sdk, workdir=home, registry=registry())
        Engine(model="fake-model", client=sdk, workdir=home, registry=registry())  # an explore child's share
        assert eng._instrumented
        assert sdk._client.event_hooks["request"].count(loop_mod._count_attempt) == 1, "hook installed once"
        events = [e async for e in eng.run_turn("read it")]
        assert sends == ["0", "1", "0"], sends  # the SDK retried once; its own counter agrees
        started, _ = pairs(events)
        (call,) = started.values()
        assert call.envelope.attempt == 2, call.envelope
        requests = notes(eng, "request")
        assert [(r["attempts"], r["attempts_observed"]) for r in requests] == [(2, True), (1, True)], requests
        assert notes(eng, "call")[0]["attempt"] == 2
        # A second request on the same client, outside the loop's window, is not counted against it.
        assert loop_mod._REQUEST.get() is None
    print("SDK retry: two physical sends, one request, one logical call, one receipt ✓")


async def catalog_generation():
    eng = engine([calls(("c1", "read")), text("done")])
    runtime = Runtime(eng, {"language": "en"})
    before = eng.history[0]["content"]
    assert runtime.publish_registry({"extra": Tool("extra", schema("extra"), read, risk="safe")},
                                    reason="test") == 1
    assert eng.registry_generation == 1 and "extra" in eng.registry
    assert "extra" in eng.history[0]["content"] and eng.history[0]["content"] != before
    events = [e async for e in eng.run_turn("go")]
    started, _ = pairs(events)
    (call,) = started.values()
    assert call.envelope.registry_generation == 1
    assert notes(eng, "call")[0]["executed_generation"] == 1
    assert notes(eng, "request")[0]["registry_generation"] == 1
    eng.swap_registry(dict(eng.registry))
    assert eng.registry_generation == 2
    assert [(n["generation"], n["reason"]) for n in notes(eng, "registry")] == [(1, "test"), (2, "swap")]
    assert notes(eng, "registry")[0]["tools"] == ["extra"]
    print("catalog generation: publish_registry and swap_registry advance it; requests and receipts carry it ✓")


async def explore_lineage():
    metas = []

    class Child:
        def __init__(self, **kw):
            metas.append(kw["trajectory_meta"])
            self.history = [{"role": "system", "content": "x"}, {"role": "user", "content": "t"},
                            {"role": "assistant", "content": "nothing to report"}]
            self.trajectory = types.SimpleNamespace(session_id="child")

        async def run_turn(self, message):
            yield TurnFinished(steps=1, usage={})

    async def buy():
        return await explore.run_explore(task="t", role="explore", model="m", client=None, workdir=home,
                                         parent_session="parent", engine_cls=Child)
    await buy()
    assert "parent_turn" not in metas[-1] and metas[-1]["parent_session"] == "parent"
    token = CURRENT_CALL.set(Envelope(turn="T", request="R", logical_call_id="R.1"))
    try:
        await buy()
    finally:
        CURRENT_CALL.reset(token)
    assert {k: metas[-1][k] for k in ("parent_session", "parent_turn", "parent_request", "parent_call")} == \
        {"parent_session": "parent", "parent_turn": "T", "parent_request": "R", "parent_call": "R.1"}
    print("explore child records parent turn / request / call next to parent_session ✓")


def host_receipts():
    env = Envelope(session="s", turn="t", request="r", attempt=2, logical_call_id="r.1",
                   provider_call_id="c1", registry_generation=3, route={"endpoint": "e"})
    started = ToolStarted(call_id="c1", tool="read", args={"raw": "{}"}, envelope=env)
    finished = ToolFinished(call_id="c1", tool="read", output="ok", ok=True, duration_s=0.1, envelope=env)
    for ev in (started, finished):
        assert _event_to_notification("sid", ev)["params"]["envelope"]["logical_call_id"] == "r.1"
        assert headless.event_to_line(ev)["envelope"]["attempt"] == 2
    assert _event_to_notification("sid", TurnFinished(steps=1, turn="t"))["params"]["turn"] == "t"
    assert headless.event_to_line(TurnStarted(user_message="x", turn="t")) == {"type": "turn.started", "turn": "t"}
    assert headless.event_to_line(TurnFinished(steps=1, turn="t"))["turn"] == "t"
    # Old-style events still serialize: the envelope defaults to an empty identity.
    assert headless.event_to_line(ToolStarted(call_id="c", tool="t"))["envelope"]["logical_call_id"] == ""
    print("serve notifications and exec JSONL carry the envelope and the turn id ✓")


async def main():
    await serial_parallel_refused()
    await bounded_continuation()
    await history_fork()
    await sdk_retry()
    await catalog_generation()
    await explore_lineage()
    host_receipts()
    print("CALL IDENTITY OK — serial, parallel, refused, continuation, fork, SDK retry, generation, lineage")


asyncio.run(main())
