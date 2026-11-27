"""Exec history continuation: real CLI + store + tools; fake model, no network.

The on-disk counter catches replay of a completed side effect. The fake model
asserts that it receives the old receipt and ONLY a new explicit user task.
This tests transcript continuation, not exactly-once external execution.
"""
import asyncio
import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# resolve(): macOS puts temp dirs behind the /var → /private/var symlink and the
# exec resume path resolves its workdir, so the expectation must be the real path.
ROOT = Path(tempfile.mkdtemp(prefix="rockyexec-resume-")).resolve()
os.environ["ROCKYCODE_HOME"] = str(ROOT / "home")
os.environ["ROCKYCODE_API_KEY"] = "sk-test-fake"
os.environ["ROCKYCODE_BASE_URL"] = "https://example.invalid/v1"
os.environ["ROCKYCODE_MODEL"] = "fake"
PROJECT = ROOT / "project"
PROJECT.mkdir()
OTHER = ROOT / "other"
OTHER.mkdir()
os.chdir(OTHER)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rich.text import Text
from typer.testing import CliRunner

from invariants import assert_history_api_valid
from rockycode import cli, session
from rockycode import headless
from rockycode.engine.events import ToolFinished
from rockycode.engine.tools import Tool


def chunk(content=None, calls=()):
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(
        content=content, reasoning_content=None,
        tool_calls=[SimpleNamespace(index=i, id=cid, function=SimpleNamespace(
            name=name, arguments=json.dumps(args)))
            for i, (cid, name, args) in enumerate(calls)],
    ))])


async def stream(chunks):
    from invariants import completed_chunks
    for c in completed_chunks(chunks):
        yield c


class Completions:
    def __init__(self, turns, check=None):
        self.turns = list(turns)
        self.requests = []
        self.check = check

    async def create(self, **kwargs):
        messages = copy.deepcopy(kwargs["messages"])
        assert_history_api_valid(messages)
        self.requests.append(messages)
        if self.check:
            self.check(messages)
        assert self.turns, "unexpected model call (original task replay?)"
        return stream(self.turns.pop(0))


def client(turns, check=None):
    completions = Completions(turns, check)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def tool(name, fn, risk):
    return Tool(name=name, fn=fn, risk=risk, schema={"type": "function", "function": {
        "name": name, "parameters": {"type": "object", "properties": {}}}})


commands = []


async def bash(command):
    commands.append(command)  # no real installs, deletes, pushes, or shell
    return "0\nrecorded " + command


async def write_file(path, content):
    target = PROJECT / path
    if path == "counter.txt":
        n = int(target.read_text()) if target.exists() else 0
        target.write_text(str(n + 1))
    else:
        target.write_text(content)
    return "wrote " + path


registry = {"bash": tool("bash", bash, "risky"),
            "write_file": tool("write_file", write_file, "moderate")}
runner = CliRunner()
real_run_exec = headless.run_exec


def invoke(args, fake, *, input=None, inject_registry=True):
    async def wired(**kwargs):
        kwargs["client"] = fake
        if inject_registry:
            kwargs["registry"] = registry
        return await real_run_exec(**kwargs)

    with patch.object(headless, "run_exec", wired):
        result = runner.invoke(cli.app, ["exec", *args], input=input)
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    return result, lines


def saved(rk):
    info, error = session.resolve_session(rk)
    assert info, error
    return info, session.load_history(info.path)


def responses(history):
    return {m["tool_call_id"]: m["content"] for m in history if m["role"] == "tool"}


INITIAL = "increment once, install requests, then write after.txt"
FOLLOWUP = "The increment is complete. Install requests and write after.txt only."
first = client([[chunk(calls=[
    ("effect", "write_file", {"path": "counter.txt", "content": "1"}),
    ("install", "bash", {"command": "pip install requests"}),
    ("later", "write_file", {"path": "after.txt", "content": "done"}),
])]])
res, lines = invoke(["-C", str(PROJECT), "--no-sandbox", "--allow", "git-push", INITIAL], first)
assert res.exit_code == 2, (res.output, res.exception)
assert [x["type"] for x in lines] == ["meta", "result"]
assert lines[-1]["status"] == "blocked"
assert lines[-1]["blocked_on"]["grant"] == "pkg-install"
source_id = lines[-1]["session"]
source, history = saved(source_id)
source_bytes = source.path.read_bytes()
assert source.project_id == session.get_project(PROJECT).id
assert any(i.session_id == source.session_id for i in session.list_sessions(workdir=PROJECT))
assert (PROJECT / "counter.txt").read_text() == "1"
assert not (PROJECT / "after.txt").exists() and commands == []
assert responses(history)["effect"] == "wrote counter.txt"
assert responses(history)["install"].startswith("[denied]"), responses(history)
assert "interrupted" in responses(history)["later"]
assert_history_api_valid(history)


def check_followup(messages):
    assert sum(m.get("content") == INITIAL for m in messages) == 1
    assert sum(m.get("content") == FOLLOWUP for m in messages) == 1
    assert responses(messages)["effect"] == "wrote counter.txt"
    assert responses(messages)["install"].startswith("[denied]")


# No grant: the new turn still blocks, with no repeated earlier side effect.
res, again = invoke(["--resume", source_id, "--no-sandbox", FOLLOWUP], client([
    [chunk(calls=[("retry", "bash", {"command": "pip install requests"})])],
], check_followup))
assert res.exit_code == 2 and commands == [], res.output
assert again[0]["profile"]["grants"] == [], "old git-push grant must not survive"
assert (PROJECT / "counter.txt").read_text() == "1"

# Cross-folder + registered rename: resume goes to the project's NEW location.
renamed = ROOT / "renamed"
PROJECT.rename(renamed)
PROJECT = renamed
session.get_project(PROJECT)
second = client([
    [chunk(calls=[("retry-install", "bash", {"command": "pip install requests"}),
                  ("finish", "write_file", {"path": "after.txt", "content": "done"})])],
    [chunk(content="finished follow-up")],
], check_followup)
res, continued = invoke(["--resume", source_id, "--allow", "pkg-install", "--allow", "delete",
                         "--no-sandbox", "--events", FOLLOWUP], second)
assert res.exit_code == 0, (res.output, res.exception)
assert continued[0]["resume"] == {"mode": "history-only", "requires_prompt": True,
                                  "from_session": source_id}
assert continued[0]["profile"]["grants"] == ["delete", "pkg-install"]
assert continued[0]["workdir"] == headless._scrub(str(PROJECT))
assert continued[-1]["session"] != source_id
assert continued[-1]["summary"] == "finished follow-up"
assert commands == ["pip install requests"]
assert (PROJECT / "counter.txt").read_text() == "1", "completed side effect replayed"
assert (PROJECT / "after.txt").read_text() == "done"
assert any(x["type"] == "tool.finished" for x in continued)
child, carried = saved(continued[-1]["session"])
assert source.path.read_bytes() == source_bytes, "history continuation must fork, not alter source"
assert sum(m.get("content") == INITIAL for m in carried) == 1
assert any(json.loads(line).get("data", {}).get("resumed_from") == source.session_id
           for line in child.path.read_text().splitlines())
print("blocked -> explicit granted follow-up: receipt preserved, counter remains 1, new session ✓")

# Grants don't disable unconditional command refusals.
res, refused = invoke(["--resume", child.session_id, "--allow", "delete", "--allow", "git-push",
                       "--no-sandbox", "check refusal"], client([
    [chunk(calls=[("never", "bash", {"command": "rm -rf /"})])],
    [chunk(content="refused safely")],
]))
assert res.exit_code == 0 and refused[-1]["evidence"]["refused"], res.output
assert commands == ["pip install requests"]

# A grant never adds a tool excluded by the current profile; use real registry.
res, readonly = invoke(["--resume", source_id, "--profile", "read", "--allow", "tool:write_file",
                        "inspect only"], client([[chunk(content="inspected")]]), inject_registry=False)
assert res.exit_code == 0, res.output
assert set(readonly[0]["profile"]["tools"]) == headless.PROFILE_TOOLS["read"]
assert readonly[0]["profile"]["sandbox"] is False

# CLI fail-fast: no automatic prompt, unknown/ambiguous id, conflicting workdir.
for args, expected in [
    (["--resume", source_id, "--allow", "pkg-install"], "explicit follow-up"),
    (["--resume", "rk_deadbeef", "follow up"], "no session matches"),
    (["--resume", source_id, "-C", str(OTHER), "follow up"], "conflicts"),
    (["--resume", source_id, ""], "empty task"),
]:
    fake = client([])
    with patch("rockycode.engine.sandbox.ChatSandbox.start", side_effect=AssertionError("started Docker")):
        res, emitted = invoke(args, fake)
    assert res.exit_code == 1 and expected in res.stderr, (args, res.output, res.exception)
    assert emitted == [] and fake.chat.completions.requests == []

# Explicit '-' permits stdin follow-up; no new grants inherited from child.
res, piped = invoke(["--resume", continued[-1]["session"], "--profile", "read", "-"],
                    client([[chunk(content="answer")]]), input="summarize completed work\n")
assert res.exit_code == 0 and piped[0]["profile"]["grants"] == [], res.output

# The process-level argv rewrite for chat's picker must not touch exec.
proc = subprocess.run([sys.executable, "-m", "rockycode.cli", "exec", "--resume"],
                      capture_output=True, text=True, timeout=30)
assert proc.returncode != 0 and proc.stdout == ""
assert "requires an argument" in proc.stderr and "__pick__" not in proc.stderr, proc.stderr
# Typer enables Rich styling in GitHub Actions, even with captured output.
# Styles can split an option's two hyphens; check rendered text in both modes.
for term in ("dumb", "xterm-256color"):
    help_env = {**os.environ, "GITHUB_ACTIONS": "true", "TERM": term}
    help_env.pop("_TYPER_FORCE_DISABLE_TERMINAL", None)
    help_result = subprocess.run([sys.executable, "-m", "rockycode.cli", "exec", "--help"],
                                 capture_output=True, text=True, timeout=30, env=help_env)
    assert help_result.returncode == 0, help_result.stderr
    assert ("\x1b[" in help_result.stdout) == (term != "dumb"), help_result.stdout
    help_text = Text.from_ansi(help_result.stdout).plain
    for flag in ("--resume", "--allow"):
        assert flag in help_text, help_text
    assert "phase 2" not in help_text
print("CLI validation, stdin, cross-folder rename, grant scope and current profile ✓")


async def close_after_finish(names):
    """Close exactly where the old engine lost already-completed results."""
    ran = []

    def recording(name):
        async def fn():
            ran.append(name)
            return "completed " + name
        return fn

    reg = {n: tool(n, recording(n), "safe" if n != "write_file" else "moderate") for n in names}
    engine, _ = headless.build_exec_engine(model="fake", workdir=PROJECT, registry=reg,
        client=client([[chunk(calls=[(f"c{i}", n, {}) for i, n in enumerate(names)])]]))
    gen = engine.run_turn("run batch")
    async for event in gen:
        if isinstance(event, ToolFinished):
            break
    await gen.aclose()
    history = session.load_history(engine.trajectory.path)
    assert_history_api_valid(history)
    assert ran == names
    for i, name in enumerate(names):
        assert responses(history)[f"c{i}"] == "completed " + name, history


asyncio.run(close_after_finish(["write_file"]))
asyncio.run(close_after_finish(["read_file", "grep", "glob"]))
print("generator close after ToolFinished preserves serial + all completed parallel results ✓")

# Legacy hard-kill transcripts remain history-only; no stored tool dispatch.
legacy = session.global_traj_dir() / "20000101-000000-feed0001.jsonl"
legacy_records = [
    {"kind": "meta", "data": {"workdir": str(PROJECT), "model": "fake"}},
    {"kind": "message", "data": {"role": "user", "content": "legacy task"}},
    {"kind": "message", "data": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "uncertain", "type": "function", "function": {
            "name": "bash", "arguments": '{"command":"git push origin main"}'}}]}},
]
legacy.write_text("\n".join(json.dumps(x) for x in legacy_records) + "\n")
legacy_client = client([[chunk(content="inspect external state before retrying")]])
res, recovered = invoke(["--resume", "rk_feed0001", "--profile", "read", "inspect uncertain effects"],
                         legacy_client, inject_registry=False)
assert res.exit_code == 0, res.output
assert "interrupted" in responses(legacy_client.chat.completions.requests[0])["uncertain"]
assert recovered[-1]["evidence"]["commands"] == []
assert commands == ["pip install requests"]

duplicate = legacy.with_name("20000101-000001-feed0002.jsonl")
duplicate.write_bytes(legacy.read_bytes())
res, _ = invoke(["--resume", "feed", "follow up"], client([]))
assert res.exit_code == 1 and "ambiguous" in res.stderr, res.output

# Folder gone: reject before constructing an engine (also covers legacy fallback).
PROJECT.rename(ROOT / "unregistered-move")
res, _ = invoke(["--resume", source_id, "follow up"], client([]))
assert res.exit_code == 1 and "folder is gone" in res.stderr, res.output
print("legacy repair does not replay calls; ambiguous ids and missing projects fail cleanly ✓")
print("EXEC RESUME SMOKE OK — explicit history continuation, no automatic replay")
