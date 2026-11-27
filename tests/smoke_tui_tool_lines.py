"""The transcript says what rocky did: one line per tool call, naming the file,
the command or the query (headless pilot, no API).

Before, a call was two lines — `⚒ read_file …` then `✓ read_file (0.0s) <first
output line>` — and neither said which file. In yolo mode, where no approval
card shows the command either, the user could not check what ran.
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
work = Path(tempfile.mkdtemp(prefix="rockytoollines-"))
os.chdir(work)
os.environ.setdefault("ROCKYCODE_HOME", tempfile.mkdtemp(prefix="rockytest-home-"))
os.environ["ROCKYCODE_OLLAMA_URL"] = "http://127.0.0.1:9"

from textual.widgets import Static  # noqa: E402

from invariants import completed_chunks  # noqa: E402
from rockycode.engine.approval import tool_brief  # noqa: E402
from rockycode.engine.loop import Engine  # noqa: E402
from rockycode.session import load_history  # noqa: E402
from rockycode.tui.app import RockyCodeApp  # noqa: E402
from rockycode.tui.chatinput import ChatInput  # noqa: E402

(work / "app.py").write_text("def main():\n    print('hello')\n")

# ── the summary itself ───────────────────────────────────────────────────────
for name, args, want in (
        ("bash", {"command": "python -m pytest -q\n  tests/"}, "python -m pytest -q tests/"),
        ("read_file", {"path": "app.py"}, "app.py"),
        ("read_file", {"path": "big.py", "offset": 120, "limit": 60}, "big.py:120"),
        ("write_file", {"path": "notes.md", "content": "a\nb\nc"}, "notes.md · 3 lines"),
        ("edit_file", {"path": "app.py", "old_string": "x", "new_string": "do-not-show"}, "app.py"),
        ("grep", {"pattern": "def main", "path": "src", "include": "*.py"}, "def main in src · *.py"),
        ("web_research", {"queries": ["a", "b", "c", "d"]}, "a · b · c"),
        ("explore", {"role": "explore", "task": "where is main()?"}, "explore: where is main()?"),
        ("mcp__docs__lookup", {"topic": "routing", "depth": 2}, "routing"),
        ("check_code", {}, ""),
        ("bash", {"raw": json.dumps({"command": "ls -la"})}, "ls -la"),  # the engine event's raw form
        ("bash", {"raw": "{not json"}, ""),
        ("bash", "not a dict", "")):
    assert tool_brief(name, args) == want, (name, args, tool_brief(name, args))
FAKE_KEY = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"  # built here: no key-shaped literal in the repo
secret = tool_brief("bash", {"command": f"curl -H 'Authorization: Bearer {FAKE_KEY}' https://x"})
assert FAKE_KEY[:19] not in secret and "redacted" in secret, secret
long = tool_brief("bash", {"command": "x" * 300})
assert len(long) == 96 and long.endswith("…")
assert "do-not-show" not in tool_brief("write_file", {"path": "p", "content": "do-not-show"})
print("brief: the command, the file, the query — one line, secrets masked, never the content written  ✓")


class Usage:
    def model_dump(self):
        return {"prompt_tokens": 30, "completion_tokens": 6}


def chunk(content=None, tool_calls=None, usage=None):
    delta = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(usage=usage, choices=[types.SimpleNamespace(delta=delta)])


def call(index, id_, name, args):
    return types.SimpleNamespace(index=index, id=id_, function=types.SimpleNamespace(name=name, arguments=json.dumps(args)))


async def stream_from(chunks):
    for c in completed_chunks(chunks):
        yield c


class Completions:
    """Reads two files at once (one is missing), greps, then answers."""

    def __init__(self):
        self.calls = 0
        self.hold = asyncio.Event()

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return stream_from([chunk(tool_calls=[call(0, "c1", "read_file", {"path": "app.py"}),
                                                  call(1, "c2", "read_file", {"path": "missing.py"})]),
                                chunk(usage=Usage())])
        if self.calls == 2:
            return stream_from([chunk(tool_calls=[call(0, "c3", "grep", {"pattern": "def main", "include": "*.py"})]),
                                chunk(usage=Usage())])
        return stream_from([chunk(content="done."), chunk(usage=Usage())])


def lines(app) -> list[str]:
    return [str(w.render()) for w in app._transcript().query(Static)
            if any(mark in str(w.render()) for mark in ("⚒", "✓", "✗"))]


async def main():
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=Completions()))
    engine = Engine(model="deepseek-flash", client=client, workdir=work)
    engine.evolve_enabled = False
    app = RockyCodeApp(engine, permission="yolo")
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        box = app.query_one(ChatInput)
        box.focus()
        box.text = "what does app.py do?"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.05)
            if any("done." in str(w.render()) for w in app._transcript().query("*")):
                break
        got = lines(app)
        assert len(got) == 3, got  # one line per call — the start line became the result line
        assert "✓ read_file app.py" in got[0] and "def main" in got[0], got[0]
        assert "✗ read_file missing.py" in got[1], got[1]
        assert "✓ grep def main in *.py" in got[2], got[2]
        assert not any("⚒" in line for line in got), "no start line is left behind"
        assert not app._tool_lines, "and nothing is kept for a call that ended"
        print("live: one line per call, naming the file or pattern; a failed call keeps its target  ✓")

    # A resumed session replays the calls with their targets too.
    app2 = RockyCodeApp(Engine(model="deepseek-flash", client=client, workdir=work), permission="yolo")
    async with app2.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        await app2._replay(load_history(engine.trajectory.path))
        await pilot.pause(0.2)
        replay = [line for line in lines(app2) if "⚒" in line]
        assert len(replay) == 3, replay
        assert "read_file app.py" in replay[0] and "read_file missing.py" in replay[1] and "grep def main in *.py" in replay[2], replay
        print("resume: replayed calls name their targets  ✓")

asyncio.run(main())
print("TUI TOOL LINES OK — rocky shows the work. amaze!")
