"""Resume handoff: rk_ session ids, titles (trajectory record + flash
generation), the cross-folder registry lookup, and the --resume CLI path.
No tty, no network."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

_HOME = Path(tempfile.mkdtemp(prefix="rockytest-resume-"))
os.environ["ROCKYCODE_HOME"] = str(_HOME)  # BEFORE imports — read at import time

from rockycode import session  # noqa: E402
from rockycode.engine.titler import generate_title  # noqa: E402
from rockycode.engine.trajectory import TrajectoryLogger  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TRAJ = _HOME / "trajectories"


def write_traj(stem: str, *, project_id="p1", name="rocky", workdir="/tmp/rocky",
               titles=()) -> Path:
    TRAJ.mkdir(parents=True, exist_ok=True)
    recs = [
        {"t": time.time(), "kind": "meta",
         "data": {"model": "m", "project_id": project_id, "project_name": name, "workdir": workdir}},
        {"t": 0, "kind": "message", "data": {"role": "system", "content": "sys"}},
        {"t": 0, "kind": "message", "data": {"role": "user", "content": "fix the login bug"}},
        {"t": 0, "kind": "message", "data": {"role": "assistant", "content": "done"}},
    ]
    recs += [{"t": 0, "kind": "title", "data": {"title": t}} for t in titles]
    p = TRAJ / f"{stem}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    return p


write_traj("20260707-153012-abcd1111")
write_traj("20260706-120000-abcd2222")
write_traj("20260705-110000-ef012345", titles=("old name", "credential fix"))

# public id: rk_ + hash tail of the stem
assert session.public_id("20260707-153012-abcd1111") == "rk_abcd1111"
print("public_id: rk_<hash tail>  ✓")

# titles: last record wins; untitled falls back to first user message
infos = {i.session_id: i for i in session.list_sessions("all")}
assert infos["20260705-110000-ef012345"].title == "credential fix"
assert infos["20260705-110000-ef012345"].display_title == "credential fix"
assert infos["20260707-153012-abcd1111"].display_title == "fix the login bug"
assert session.list_sessions("all", query="credential")[0].session_id.endswith("ef012345")
print("titles: last record wins, summary fallback, searchable  ✓")

# resolve: rk_/bare/prefix/legacy-stem forms; ambiguity + unknown are errors
ok, err = session.resolve_session("rk_ef012345")
assert ok and ok.session_id.endswith("ef012345") and not err
ok, err = session.resolve_session("ef01")
assert ok and ok.session_id.endswith("ef012345"), err
ok, err = session.resolve_session("20260707-153012-abcd1111")
assert ok and ok.session_id == "20260707-153012-abcd1111"
ok, err = session.resolve_session("abcd")
assert ok is None and "ambiguous" in err and "rk_abcd1111" in err and "rk_abcd2222" in err, err
ok, err = session.resolve_session("rk_deadbeef")
assert ok is None and "no session matches" in err, err
print("resolve_session: rk_/bare/prefix/legacy, ambiguous + unknown error  ✓")

# cross-folder: registry maps project id → current path (rename-proof)
live = Path(tempfile.mkdtemp(prefix="rockytest-proj-"))
(_HOME / "projects.json").write_text(json.dumps({
    "p1": {"current": str(live), "paths": ["/gone/old", str(live)]},
    "p2": {"current": "/gone/new", "paths": ["/gone/older"]},
}))
assert session.project_current_path("p1") == live
assert session.project_current_path("p2") is None  # every known path is gone
assert session.project_current_path("p404") is None
print("project_current_path: current, fallback, missing  ✓")

# trajectory writer: .title() appends a readable record
log = TrajectoryLogger({"model": "m", "project_id": "p9", "project_name": "x", "workdir": "/tmp"})
log.message({"role": "user", "content": "hello"})
log.title("hello world named")
got = session._read_info(log.path)
assert got is not None and got.title == "hello world named", got
print("TrajectoryLogger.title: record written and read back  ✓")


# titler: cleans quotes/periods/newlines; any failure → None
class _FakeClient:
    def __init__(self, reply=None, boom=False):
        async def create(**kw):
            if boom:
                raise RuntimeError("no api")
            msg = SimpleNamespace(content=reply)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


t = asyncio.run(generate_title(_FakeClient('"Fix Login Bug."\nextra'), "u", "a"))
assert t == "Fix Login Bug", t
assert asyncio.run(generate_title(_FakeClient(boom=True), "u", "a")) is None
assert asyncio.run(generate_title(_FakeClient(""), "u", "a")) is None
print("generate_title: cleaned title; failures → None, never raise  ✓")

# CLI: `rockycode --resume <unknown id>` reaches chat (argv inject) and fails
# cleanly before any engine/network — proving the top-level flag route works
env = {**os.environ,
       "ROCKYCODE_HOME": tempfile.mkdtemp(prefix="rockytest-empty-"),
       "ROCKYCODE_API_KEY": "sk-test-fake", "ROCKYCODE_MODEL": "fake-model"}
r = subprocess.run([sys.executable, "rockycode/cli.py", "--resume", "rk_deadbeef"],
                   capture_output=True, text=True, cwd=REPO_ROOT, env=env, timeout=60)
out = r.stdout + r.stderr
assert r.returncode != 0 and "no session matches" in out, out[:400]
print("cli: top-level --resume <id> routes to chat, unknown id fails cleanly  ✓")

# exit card: id + title + folder + full copy-paste commands; silent when the
# session never had a user turn
import contextlib
import io

from rockycode import cli as rockycli

eng = SimpleNamespace(trajectory=SimpleNamespace(path=TRAJ / "20260705-110000-ef012345.jsonl"))
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rockycli._print_exit_card(eng)
card = buf.getvalue()
for needle in ("♪ session saved", "rk_ef012345", "credential fix", "📁 rocky",
               "rockycode --resume rk_ef012345", "rockycode --resume"):
    assert needle in card, (needle, card)
empty = TRAJ / "20260701-000000-99999999.jsonl"
empty.write_text(json.dumps({"t": 0, "kind": "meta", "data": {
    "model": "m", "project_id": "p1", "project_name": "rocky", "workdir": "/tmp/rocky"}}) + "\n")
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    rockycli._print_exit_card(SimpleNamespace(trajectory=SimpleNamespace(path=empty)))
assert buf2.getvalue() == "", buf2.getvalue()
print("exit card: id/title/folder + commands; silent with no user turn  ✓")

print("RESUME HANDOFF SMOKE OK — an id to come back to. amaze!")
