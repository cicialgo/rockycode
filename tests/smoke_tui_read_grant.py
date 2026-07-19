"""Read-grant flow (headless Textual pilot, no API).

The bug: an out-of-workdir read_file used to be APPROVED yet still blocked by the
hard jail — the approval was a lie. Now approving one records the path in
engine.read_grants, so the read actually runs AND re-reads don't re-prompt.
Secret files stay refused even inside a granted path.
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

WORK = Path(tempfile.mkdtemp(prefix="rockyrg-work-")).resolve()
os.chdir(WORK)
REFDIR = Path(tempfile.mkdtemp(prefix="rockyrg-ref-")).resolve()   # OUTSIDE the workdir
REF = REFDIR / "reference.py"
REF.write_text("# reference notes\nMAGIC = 42\n")
SECRET = REFDIR / ".env"
SECRET.write_text("API_KEY=supersecret\n")

from textual.widgets import Static

from rockycode.engine.loop import Engine
from rockycode.engine.tools import build_registry
from rockycode.tui.app import ChatInput, RockyCodeApp
from rockycode.tui.permission import InlineApproval


class U:
    def model_dump(self):
        return {"prompt_tokens": 30, "completion_tokens": 6}


def chunk(content=None, tool_calls=None, usage=None):
    d = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(usage=usage, choices=[types.SimpleNamespace(delta=d)])


def tc(i, id_, name, args):
    return types.SimpleNamespace(index=i, id=id_, function=types.SimpleNamespace(name=name, arguments=args))


async def stream(chunks):
    for c in chunks:
        yield c


class FakeCompletions:
    """Read the OUTSIDE reference file twice (same turn), then answer. The 2nd
    read must NOT re-prompt — if it did, the turn would hang waiting for approval."""
    def __init__(self, path):
        self.path = path
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls in (1, 2):
            return stream([
                chunk(tool_calls=[tc(0, f"r{self.calls}", "read_file", json.dumps({"path": self.path}))]),
                chunk(usage=U()),
            ])
        return stream([chunk(content="done."), chunk(usage=U())])


def build(path):
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions(path)))
    eng = Engine(model="fake", client=client, workdir=WORK, registry=build_registry(WORK, read_grants=set()))
    # keep the registry's grant set the SAME object the engine mutates
    eng.registry = build_registry(WORK, read_grants=eng.read_grants)
    return RockyCodeApp(eng, permission="ask"), eng


def has_approval(app):
    return len(app.query(InlineApproval)) > 0


def tool_results(eng):
    return [m["content"] for m in eng.history if m["role"] == "tool"]


async def wait_until(pilot, cond, timeout=6.0, step=0.05):
    for _ in range(int(timeout / step)):
        await pilot.pause(step)
        if cond():
            return True
    return bool(cond())


async def main():
    # --- approve an out-of-jail read → it runs, grant recorded, re-read silent ---
    app, eng = build(str(REF))
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause(0.1)
        inp = app.query_one(ChatInput)
        inp.focus()
        inp.text = "read the reference"
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: has_approval(app)), "out-of-jail read must prompt"
        await pilot.press("y")   # approve once
        # the turn must FINISH after one approval (2nd read auto-allowed via the grant)
        assert await wait_until(pilot, lambda: any(
            m.get("role") == "assistant" and "done." in str(m.get("content", "")) for m in eng.history)), \
            "turn should complete after a single approval (re-read must not re-prompt)"
        assert REF.resolve() in eng.read_grants, f"the read should be granted: {eng.read_grants}"
        results = tool_results(eng)
        assert len(results) == 2, f"both reads should have a result: {results}"
        assert all("MAGIC = 42" in r for r in results), f"the grant must let the read SUCCEED: {results}"

    # --- a secret file is refused even when its path is granted ---
    app, eng = build(str(SECRET))
    eng.read_grants.add(SECRET.resolve())   # pretend it was granted
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause(0.1)
        inp = app.query_one(ChatInput)
        inp.focus()
        inp.text = "read the env"
        await pilot.press("enter")
        # granted → no prompt, but the read itself refuses the secret
        assert await wait_until(pilot, lambda: any(
            "refusing to read" in r for r in tool_results(eng))), "a granted secret file must still be refused"
        assert not any("supersecret" in r for r in tool_results(eng)), "secret value must never leak"

    print("TUI READ-GRANT SMOKE OK — approve widens the read jail (runs + no re-prompt); secrets still refused. amaze!")


asyncio.run(main())
