"""Container-glue smoke test without Docker: LocalSession in a temp git repo,
fake model stream drives Engine through read→edit→test, then extract_patch.
Verifies the whole `--runner rockycode` flow except `docker exec` itself.
"""
import asyncio
import subprocess
import tempfile
import types
from pathlib import Path

from rockycode.engine.container import LocalSession, build_session_registry, extract_patch
from rockycode.engine.events import ToolFinished, TurnFinished
from rockycode.engine.loop import Engine


def chunk(content=None, tool_calls=None, usage=None):
    if usage is not None and content is None and tool_calls is None:
        return types.SimpleNamespace(usage=usage, choices=[])
    delta = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(usage=None, choices=[types.SimpleNamespace(delta=delta)])


def tc(index, id_, name, args):
    return types.SimpleNamespace(
        index=index, id=id_, function=types.SimpleNamespace(name=name, arguments=args))


async def stream_from(chunks):
    for c in chunks:
        yield c


class FakeCompletions:
    """Scripted agent: read buggy.py → edit the bug → run the file → answer."""

    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        import json
        if self.calls == 1:
            args = json.dumps({"path": "buggy.py"})
            return stream_from([chunk(tool_calls=[tc(0, "c1", "read_file", args)])])
        if self.calls == 2:
            args = json.dumps({
                "path": "buggy.py",
                "old_string": "return a - b  # bug",
                "new_string": "return a + b",
            })
            return stream_from([chunk(tool_calls=[tc(0, "c2", "edit_file", args)])])
        if self.calls == 3:
            args = json.dumps({"command": "python buggy.py"})
            return stream_from([chunk(tool_calls=[tc(0, "c3", "bash", args)])])
        return stream_from([chunk(content="DONE. fixed add(). amaze!")])


async def main():
    workdir = Path(tempfile.mkdtemp(prefix="rockycontainer-"))
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.email", "r@r"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.name", "rocky"], cwd=workdir, check=True)
    (workdir / "buggy.py").write_text(
        "def add(a, b):\n    return a - b  # bug\n\nassert add(1, 2) == 3\nprint('ok')\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=workdir, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=workdir, check=True)

    session = LocalSession(workdir)
    fake_client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=FakeCompletions()))

    engine = Engine(
        model="fake-model",
        client=fake_client,
        registry=build_session_registry(session),
        trajectory_meta={"runner": "rockycode", "instance_id": "smoke__test-1"},
    )

    tool_results = []
    steps = 0
    async for ev in engine.run_turn("fix the bug in buggy.py"):
        if isinstance(ev, ToolFinished):
            tool_results.append(ev)
        elif isinstance(ev, TurnFinished):
            steps = ev.steps

    assert steps == 4, f"expected 4 steps, got {steps}"
    by_tool = {t.tool: t for t in tool_results}
    assert "return a - b" in by_tool["read_file"].output
    assert by_tool["edit_file"].ok, by_tool["edit_file"].output
    assert "ok" in by_tool["bash"].output and "[exit 0]" in by_tool["bash"].output

    patch = await extract_patch(session)
    assert "buggy.py" in patch and "+    return a + b" in patch, patch
    assert "-    return a - b  # bug" in patch

    # the patch must apply cleanly to a fresh checkout — same as swebench will
    subprocess.run(["git", "stash", "-q"], cwd=workdir, check=True)
    proc = subprocess.run(
        ["git", "apply", "--cached", "-"], cwd=workdir, input=patch.encode(), capture_output=True
    )
    assert proc.returncode == 0, proc.stderr.decode()

    print("steps:", steps, "| tools:", [t.tool for t in tool_results])
    print("patch lines:", len(patch.splitlines()))
    print("CONTAINER SMOKE OK — amaze amaze amaze!")


asyncio.run(main())
