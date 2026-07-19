"""Headless exec mode (engine/headless.py) — the agent-to-agent contract.

Asserts, with a fake DeepSeek stream and recording tools (no API, no network):
  1. done:    every emitted line is JSON-serializable (the stdout purity
              contract), meta is FIRST with schema + rk_ session, result is
              LAST with status=done, evidence carries the command + changed
              file, exit 0, history stays API-valid.
  2. blocked: an ask-tier command (pip install) stops the run — exit 2,
              blocked_on.grant == 'pkg-install', the tool never ran, and the
              broken-out generator still leaves history API-valid.
  3. refused: a block-tier command (rm -rf /) is denied but the run CONTINUES
              — model adapts, answers, exit 0, refusal in evidence.
  4. delete:  `rm -rf build/` is allow-tier for goal mode but needs the
              'delete' grant headless — exit 2; with the grant it runs.
  5. scrub:   home paths never cross stdout.
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
os.environ.setdefault("ROCKYCODE_HOME", tempfile.mkdtemp(prefix="rockyexec-home-"))
os.chdir(tempfile.mkdtemp(prefix="rockyexec-"))

from rockycode.engine.headless import (
    EXIT_BLOCKED,
    EXIT_DONE,
    SCHEMA,
    HeadlessApprover,
    _scrub,
    build_exec_engine,
    drive,
)
from rockycode.engine.tools import Tool
from invariants import assert_history_api_valid


class FakeUsage:
    def model_dump(self):
        return {"prompt_tokens": 40, "completion_tokens": 8}


def chunk(content=None, tool_calls=None, usage=None):
    delta = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(usage=usage, choices=[types.SimpleNamespace(delta=delta)])


def tc(index, id_, name, args):
    return types.SimpleNamespace(
        index=index, id=id_, function=types.SimpleNamespace(name=name, arguments=args))


async def stream_from(chunks):
    for c in chunks:
        yield c


class FakeCompletions:
    """Scripted turns: each entry is the list of chunks one API call streams."""
    def __init__(self, turns):
        self.turns = turns
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        return stream_from(self.turns[min(self.calls, len(self.turns)) - 1])


_BASH_SCHEMA = {"type": "function",
                "function": {"name": "bash", "parameters": {"type": "object", "properties": {}}}}
_WRITE_SCHEMA = {"type": "function",
                 "function": {"name": "write_file", "parameters": {"type": "object", "properties": {}}}}


def make_engine(turns, grants=frozenset()):
    ran = {"bash": [], "write_file": []}

    async def bash_fn(command):
        ran["bash"].append(command)
        return f"0\nran: {command}"

    async def write_fn(path, content):
        ran["write_file"].append(path)
        return f"wrote {path}"

    registry = {
        "bash": Tool(name="bash", schema=_BASH_SCHEMA, fn=bash_fn, risk="risky"),
        "write_file": Tool(name="write_file", schema=_WRITE_SCHEMA, fn=write_fn, risk="moderate"),
    }
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions(turns)))
    engine, approver = build_exec_engine(
        model="fake", workdir=Path.cwd(), grants=grants,
        client=client, registry=registry,
    )
    return engine, approver, ran


def run(turns, grants=frozenset()):
    engine, approver, ran = make_engine(turns, grants)
    lines: list[dict] = []
    code = asyncio.run(drive(engine, approver, "do the task", write=lines.append))
    for ln in lines:  # the purity contract: every line must survive json round-trip
        json.loads(json.dumps(ln, default=str))
    return code, lines, engine, ran


# three deltas on purpose: the stream must coalesce them into ONE text event
FINAL = [chunk(content="all "), chunk(content="do"), chunk(content="ne."), chunk(usage=FakeUsage())]


# 1. done ---------------------------------------------------------------------
code, lines, engine, ran = run([
    [chunk(tool_calls=[tc(0, "c1", "bash", '{"command":"echo hi"}'),
                       tc(1, "c2", "write_file", '{"path":"out.txt","content":"x"}')]),
     chunk(usage=FakeUsage())],
    FINAL,
])
assert code == EXIT_DONE, code
meta, result = lines[0], lines[-1]
assert meta["type"] == "meta" and meta["schema"] == SCHEMA, meta
assert meta["session"].startswith("rk_"), meta
assert meta["profile"]["mode"] == "workspace-write"
# the profile must state the isolation posture the caller is trusting
assert "sandbox" in meta["profile"] and "network" in meta["profile"], meta["profile"]
assert result["type"] == "result" and result["status"] == "done", result
assert result["summary"] == "all done."
assert result["evidence"]["commands"] == [{"command": "echo hi", "ok": True}]
assert result["evidence"]["files_changed"] == ["out.txt"]
assert ran["bash"] == ["echo hi"] and ran["write_file"] == ["out.txt"]
assert any(l["type"] == "turn.finished" for l in lines)
texts = [l for l in lines if l["type"] == "text"]
assert texts == [{"type": "text", "text": "all done."}], \
    "deltas must coalesce into one text event per block: " + repr(texts)
from rockycode.engine.headless import _clip_output
clipped = _clip_output("x" * 5000)
assert clipped["output_truncated"] and clipped["output_chars"] == 5000 \
    and len(clipped["output"]) == 2000, "tool output is a receipt, not a payload"
assert _clip_output("short") == {"output": "short"}
assert_history_api_valid(engine.history)

# 2. blocked (ask tier) -------------------------------------------------------
code, lines, engine, ran = run([
    [chunk(tool_calls=[tc(0, "c1", "bash", '{"command":"pip install requests"}')]),
     chunk(usage=FakeUsage())],
    FINAL,
])
assert code == EXIT_BLOCKED, code
result = lines[-1]
assert result["status"] == "blocked", result
assert result["blocked_on"]["grant"] == "pkg-install", result["blocked_on"]
assert ran["bash"] == [], "ask-tier command must not run without a grant"
assert_history_api_valid(engine.history)  # broken-out generator still repaired

# 3. refused (block tier) — run continues -------------------------------------
code, lines, engine, ran = run([
    [chunk(tool_calls=[tc(0, "c1", "bash", '{"command":"rm -rf /"}')]),
     chunk(usage=FakeUsage())],
    FINAL,
])
assert code == EXIT_DONE, code
result = lines[-1]
assert result["status"] == "done"
assert result["evidence"]["refused"] and "rm -rf /" in result["evidence"]["refused"][0]["command"]
assert ran["bash"] == [], "block-tier command must never run"
assert_history_api_valid(engine.history)

# 4. delete needs a grant headless --------------------------------------------
code, lines, engine, ran = run([
    [chunk(tool_calls=[tc(0, "c1", "bash", '{"command":"rm -rf build/"}')]),
     chunk(usage=FakeUsage())],
    FINAL,
])
assert code == EXIT_BLOCKED, code
assert lines[-1]["blocked_on"]["grant"] == "delete", lines[-1]["blocked_on"]
assert ran["bash"] == []

code, lines, engine, ran = run([
    [chunk(tool_calls=[tc(0, "c1", "bash", '{"command":"rm -rf build/"}')]),
     chunk(usage=FakeUsage())],
    FINAL,
], grants=frozenset({"delete"}))
assert code == EXIT_DONE, code
assert ran["bash"] == ["rm -rf build/"], "granted delete must run"

# approver unit: grant token equals the safety pattern name -------------------
appr = HeadlessApprover({}, grants=frozenset({"git-push"}))
assert asyncio.run(appr("bash", {"command": "git push origin main"})) is True
appr = HeadlessApprover({})
assert asyncio.run(appr("bash", {"command": "git push origin main"})) is False
assert appr.blocked["grant"] == "git-push"

# 5. scrub: home path never crosses stdout ------------------------------------
home = str(Path.home())
assert home not in _scrub(f"error at {home}/secrets/x.py"), "home path must be redacted"

# F5: the result-envelope evidence fields (which the CALLER parses) are scrubbed,
# not just the event stream — a token in a command URL must not leak in cleartext
from rockycode.engine.headless import _scrub_obj
leaky = {"command": "git push https://x:ghp_ABCDEFGHIJKLMNOPQRST1234@github.com/o/r"}
scrubbed = _scrub_obj([leaky])
assert "ghp_ABCDEFGHIJKLMNOPQRST1234" not in json.dumps(scrubbed), scrubbed
assert _scrub_obj({"a": [home + "/x"]})["a"][0] != home + "/x", "nested home path scrubbed"
print("smoke_exec: envelope evidence fields scrubbed (F5)  ✓")

# sandbox posture: the classifier is now defense-in-depth, but the REAL boundary
# is that with --sandbox every tool runs in the container — a bash `rm -rf /`
# hits the container's root, not the host. We can't spin Docker in the smoke,
# so assert the contract wiring: run_exec defaults sandbox=True, and the meta
# profile reports the posture the caller is trusting (checked above via
# meta["profile"]["sandbox"/"network"]). Confirm the default is safe:
import inspect
from rockycode.engine.headless import run_exec
sig = inspect.signature(run_exec)
assert sig.parameters["sandbox"].default is True, "exec must default to sandboxed"
assert sig.parameters["network"].default is False, "exec must default to network off"
from rockycode.engine.sandbox import ChatSandbox
assert inspect.signature(ChatSandbox.start).parameters["network"].default is False, \
    "the sandbox default must be offline everywhere"
print("sandbox: exec defaults on + offline; ChatSandbox default offline  ✓")

print("smoke_exec: OK")
