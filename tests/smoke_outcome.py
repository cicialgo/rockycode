"""Heuristic outcome capture (self-evolve phase 0) — fake DeepSeek stream, no API.

A session's trajectory ends with ONE `outcome` record (source="heuristic")
counting turns / steps / tool calls / errors / denials / test runs, written by
Engine.finalize_outcome(): idempotent, and skipped entirely for a session with
no user turn — an open-and-close session must not grow a reward line.
"""
import asyncio
import json
import os
import tempfile
import types
from pathlib import Path

os.environ.setdefault("ROCKYCODE_HOME", tempfile.mkdtemp(prefix="rockyhome-"))
os.chdir(tempfile.mkdtemp(prefix="rockyoutcome-"))

from rockycode.engine.loop import Engine
from rockycode.engine.outcome import TEST_CMD_RE, SessionStats
from rockycode.engine.tools import Tool, _fn_schema


class U:
    def model_dump(self):
        return {"prompt_tokens": 50, "completion_tokens": 10}


def chunk(content=None, tool_calls=None, usage=None):
    d = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(usage=usage, choices=[] if d.content is None and d.tool_calls is None and usage else [types.SimpleNamespace(delta=d)])


def tc(i, id_, name, args):
    return types.SimpleNamespace(index=i, id=id_, function=types.SimpleNamespace(name=name, arguments=args))


async def stream(chunks):
    for c in chunks:
        yield c


class FakeCompletions:
    """Turn 1: pytest-pass + pytest-fail + a crashing tool, then an answer.
    Turn 2: one bash call the approver will DENY, then an answer."""

    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return stream([
                chunk(tool_calls=[
                    tc(0, "t1", "bash", json.dumps({"command": "pytest -q tests_good"})),
                    tc(1, "t2", "bash", json.dumps({"command": "pytest -q tests_bad"})),
                    tc(2, "t3", "boom", "{}"),
                ]),
                chunk(usage=U()),
            ])
        if self.calls == 3:
            return stream([
                chunk(tool_calls=[tc(0, "t4", "bash", json.dumps({"command": "rm -rf scratch"}))]),
                chunk(usage=U()),
            ])
        return stream([chunk(content="done."), chunk(usage=U())])


async def _bash(command: str) -> str:
    # Mirrors the real bash tool's contract: "[exit N]" on the first line, and
    # execute() only marks [error]/[timeout] as not-ok — a failing command is ok=True.
    return "[exit 0]\n1 passed" if "good" in command else "[exit 1]\n1 failed"


async def _boom() -> str:
    return "[error] kaboom"


def build_engine():
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions()))
    registry = {
        "bash": Tool(name="bash", schema=_fn_schema("bash", "run", {"command": {"type": "string"}}, ["command"]), fn=_bash),
        "boom": Tool(name="boom", schema=_fn_schema("boom", "boom", {}, []), fn=_boom),
    }
    return Engine(model="fake", client=client, workdir=Path.cwd(), registry=registry)


async def main():
    # --- the regex knows common runners, and ignores non-test bash ---
    for cmd in ("pytest -q", "npm test", "npm run test", "cargo test", "go test ./...", "tox -e py311"):
        assert TEST_CMD_RE.search(cmd), f"should match: {cmd}"
    assert not TEST_CMD_RE.search("ls -la && git status"), "plain bash must not count as a test"

    eng = build_engine()

    async def deny_rm(name, args):
        return not (name == "bash" and "rm " in args.get("command", ""))

    eng.approver = deny_rm

    async for _ in eng.run_turn("run the tests"):
        pass
    async for _ in eng.run_turn("clean up scratch"):
        pass

    s = eng.stats
    assert s.turns == 2 and s.steps == 4, (s.turns, s.steps)
    assert s.tool_calls == 3, s.tool_calls          # executed only — the denied call is not a tool run
    assert s.tool_errors == 1, s.tool_errors        # boom's [error]
    assert s.bash_nonzero == 1, s.bash_nonzero      # the failing pytest (exit 1, still ok=True)
    assert s.tests_run == 2 and s.tests_passed == 1, (s.tests_run, s.tests_passed)
    assert s.denials == 1 and s.plan_denials == 0, (s.denials, s.plan_denials)
    assert s.interrupts == 0 and s.engine_errors == 0
    assert s.usage.get("prompt_tokens", 0) > 0, "usage should accumulate across calls"
    print("outcome: counters land on the exact loop branches  ✓")

    # --- finalize: one record, idempotent ---
    data = eng.finalize_outcome()
    assert data is not None and data["source"] == "heuristic"
    assert data["tests"] == {"run": 2, "passed": 1}, data["tests"]
    assert eng.finalize_outcome() is None, "second finalize must be a no-op"
    lines = [json.loads(l) for l in eng.trajectory.path.read_text(encoding="utf-8").splitlines()]
    outcomes = [l for l in lines if l["kind"] == "outcome"]
    assert len(outcomes) == 1, f"exactly one outcome record, got {len(outcomes)}"
    assert outcomes[0]["data"]["denials"] == 1
    print("outcome: finalize writes ONE heuristic record, idempotent  ✓")

    # --- a session with no user turn writes NO outcome ---
    eng2 = build_engine()
    assert eng2.finalize_outcome() is None, "no-turn session must skip the outcome"
    lines2 = [json.loads(l) for l in eng2.trajectory.path.read_text(encoding="utf-8").splitlines()]
    assert not any(l["kind"] == "outcome" for l in lines2)
    print("outcome: open-and-close session stays outcome-free  ✓")

    # --- feedback records: written with the local-only flag, readable back ---
    eng.trajectory.feedback({"mood": "good", "text": "汉字 ok", "local_only": True})
    lines = [json.loads(l) for l in eng.trajectory.path.read_text(encoding="utf-8").splitlines()]
    fb = [l for l in lines if l["kind"] == "feedback"]
    assert len(fb) == 1 and fb[0]["data"]["mood"] == "good" and fb[0]["data"]["local_only"] is True
    print("outcome: feedback record lands with local_only  ✓")

    # --- SessionStats is inert until something happens ---
    assert SessionStats().as_data()["tests"] == {"run": 0, "passed": 0}

    print("OUTCOME SMOKE OK — the reward line exists. amaze!")


asyncio.run(main())
