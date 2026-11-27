"""Reflect — the evolve v2 capture step (dream/reflect.py + engine hooks). No API.

Contracts under test:
  - parse_reflection: fixed-weight score in code; any missing/bad angle voids
    the WHOLE reflection (a partial grade would skew the aggregate); pattern
    items are validated field-by-field and junk is dropped, not fatal.
  - run_reflect rides the session's own api view: the instruction is the LAST
    message, tools go with tool_choice="none" (prefix cache kept, no tool-call
    answers), and any API/parse trouble returns None — never raises.
  - Engine triggers: OFF by default (bench guard); a turn boundary past
    the REFLECT_EVERY boundary fires ONE background checkpoint; the cap holds; the final
    reflect appends reflect + outcome(source="reflect") AFTER the heuristic
    record (last-outcome-wins upgrades the grade); trivial sessions skip.
  - Checkpoints are deltas: the second call's instruction carries the first
    checkpoint's summary.
"""
import asyncio
import json
import os
import tempfile
import types
from pathlib import Path

os.environ.setdefault("ROCKYCODE_HOME", tempfile.mkdtemp(prefix="rockyhome-"))
os.chdir(tempfile.mkdtemp(prefix="rockyreflect-"))

from rockycode.dream.reflect import (
    REFLECT_MAX_PER_SESSION, attach, attached, build_instruction, parse_reflection, run_reflect,
)
from rockycode.engine.loop import Engine

WD = Path.cwd()

GOOD = json.dumps({
    "grade": {"completion": 0.9, "adherence": 0.8, "efficiency": 0.6,
              "user_feeling": 0.7, "rationale": "task done, one retry"},
    "episode": {"task": "fix the login test", "outcome": "success",
                "worked": ["ran pytest first"], "failed": []},
    "facts": ["tests live under tests/"],
    "patterns": [
        {"kind": "habit", "reinforces": None, "name": "morning-arxiv-check",
         "description": "user fetches new arxiv matches by hand each session",
         "advice": ""},
        {"kind": "nonsense", "reinforces": "", "name": "retry-blind",
         "description": "agent reruns a failing command unchanged", "advice": "read the error first"},
        {"kind": "weakness", "name": "", "description": "no name, must drop"},
    ],
    "skills_used": ["lean-prover"],
    "summary": "Fixed the login test; user asked for arxiv papers again.",
})


# ---- parsing -------------------------------------------------------------------

def test_parse():
    p = parse_reflection(f"Sure! Here you go:\n{GOOD}")
    assert p is not None
    assert p["score"] == round(0.4 * 0.9 + 0.25 * 0.8 + 0.15 * 0.6 + 0.2 * 0.7, 4) == 0.79
    assert p["episode"]["task"] == "fix the login test"
    assert p["facts"] == ["tests live under tests/"]
    # junk kind coerces to weakness, the null reinforces stays None, the
    # nameless third item is dropped
    assert [it["kind"] for it in p["patterns"]] == ["habit", "weakness"]
    assert p["patterns"][0]["reinforces"] is None
    assert p["skills_used"] == ["lean-prover"]
    print("parse: score in code, junk patterns dropped  ✓")

    missing = json.loads(GOOD)
    del missing["grade"]["efficiency"]
    assert parse_reflection(json.dumps(missing)) is None, "partial grade must void"
    assert parse_reflection("no json here") is None
    assert parse_reflection('{"grade": "not a dict"}') is None
    print("parse: partial/malformed grades void the whole reflection  ✓")

    ins = build_instruction(known_patterns=[("retry-blind", "reruns blindly")],
                            prior_summary="did a thing")
    assert "[retry-blind]" in ins and "PREVIOUS CHECKPOINT" in ins and "did a thing" in ins
    ins0 = build_instruction(known_patterns=[], prior_summary=None)
    assert "(none)" in ins0 and "PREVIOUS CHECKPOINT" not in ins0
    print("instruction: known patterns + delta note ride the last message  ✓")


# ---- fakes ---------------------------------------------------------------------

class FakeUsage:
    def model_dump(self):
        return {"prompt_tokens": 50, "completion_tokens": 10}


def _chunk(content=None, usage=None):
    if content is None:
        return types.SimpleNamespace(usage=usage, choices=[])
    delta = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=None)
    return types.SimpleNamespace(usage=usage, choices=[types.SimpleNamespace(delta=delta)])


async def _stream():
    yield _chunk(content="ok, done.")
    yield _chunk(usage=FakeUsage())


class FakeCompletions:
    """Streams the main-loop calls; answers reflect (non-stream) with GOOD."""

    def __init__(self):
        self.reflect_kwargs = []

    async def create(self, **kw):
        if kw.get("stream"):
            return _stream()
        self.reflect_kwargs.append(kw)
        msg = types.SimpleNamespace(content=GOOD)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


def make_engine(fc=None):
    fc = fc or FakeCompletions()
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=fc))
    engine = Engine(model="fake-v4", client=client, workdir=WD,
                    trajectory_meta={"source": "chat", "project_id": "p-test"})
    return engine, fc


def records(engine):
    return [json.loads(l) for l in
            Path(engine.trajectory.path).read_text(encoding="utf-8").splitlines()]


async def drive_turn(engine, text="hi rocky"):
    async for _ in engine.run_turn(text):
        pass


# ---- engine triggers -----------------------------------------------------------

async def main():
    test_parse()

    # bench guard: evolve is OFF unless an entry point opts in
    engine, fc = make_engine()
    await drive_turn(engine)
    assert attached(engine) is None and fc.reflect_kwargs == []
    assert await engine.run_final_hooks() == [], "default engine must never reflect"
    print("engine: evolve off by default — no checkpoint, no final  ✓")

    # message trigger: turn boundary past REFLECT_EVERY fires ONE checkpoint
    engine, fc = make_engine()
    hook = attach(engine, every=4)
    await drive_turn(engine, "first")
    assert hook.task is None, "below threshold — must not fire"
    await drive_turn(engine, "second")   # history: sys + 2*(user+assistant) = 5
    assert hook.task is not None, "threshold crossed — must fire"
    await hook.task
    kw = fc.reflect_kwargs[0]
    assert "tool_choice" in kw and kw["tool_choice"] == "none"
    assert "stream" not in kw
    assert "Reflection checkpoint" in kw["messages"][-1]["content"]
    assert kw["extra_body"] == {"thinking": {"type": "disabled"}}
    refl = [r["data"] for r in records(engine) if r["kind"] == "reflect"]
    assert len(refl) == 1 and refl[0]["seq"] == 1 and refl[0]["final"] is False
    assert refl[0]["score"] == 0.79
    print("engine: REFLECT_EVERY boundary fires one background checkpoint  ✓")

    # final: outcome(source="reflect") lands AFTER the heuristic record, and
    # the second instruction carries the first checkpoint's summary (delta)
    engine.finalize_outcome()
    out = (await engine.run_final_hooks())[0]
    assert out is not None and out["final"] is True and out["seq"] == 2
    assert "PREVIOUS CHECKPOINT" in fc.reflect_kwargs[1]["messages"][-1]["content"]
    outs = [r["data"] for r in records(engine) if r["kind"] == "outcome"]
    assert [o["source"] for o in outs] == ["heuristic", "reflect"], outs
    assert outs[-1]["score"] == 0.79
    assert await engine.run_final_hooks() == [None], "final must be once-only"
    print("engine: final reflect supersedes the heuristic outcome, once  ✓")

    # trivial gate + cap
    engine, fc = make_engine()
    attach(engine)
    assert await engine.run_final_hooks() == [None] and fc.reflect_kwargs == []
    print("engine: trivial session (no turns) skips the final call  ✓")

    engine, fc = make_engine()
    hook = attach(engine, every=1)
    hook.seq = REFLECT_MAX_PER_SESSION
    await drive_turn(engine)
    assert hook.task is None, "cap must hold"
    print("engine: per-session checkpoint cap holds  ✓")

    # API trouble → None, session untouched
    class Boom:
        async def create(self, **kw):
            raise RuntimeError("api down")
    got = await run_reflect(types.SimpleNamespace(chat=types.SimpleNamespace(completions=Boom())),
                            "fake", [{"role": "user", "content": "x"}])
    assert got is None
    print("run_reflect: API trouble returns None, never raises  ✓")

    print("REFLECT SMOKE OK — warm checkpoints, graded exits. amaze!")


asyncio.run(main())
