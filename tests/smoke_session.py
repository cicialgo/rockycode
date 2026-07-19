"""Session storage smoke test: project identity (survives rename), cross-folder
discovery, scope/search, and history reconstruction. Uses a temp HOME so it
never touches the real ~/.rockycode."""
import json
import shutil
import tempfile
import time
from pathlib import Path

import rockycode.session as S

# redirect the global registry into a temp home
_TMP_HOME = Path(tempfile.mkdtemp(prefix="rockyhome-"))
S.HOME_ROOT = _TMP_HOME / ".rockycode"
S.REGISTRY = S.HOME_ROOT / "projects.json"


def _make_session(project_id: str, project_name: str, workdir: Path, sid: str,
                  user_msg: str, t: float, *, bench=False, goal=False):
    # all sessions live in the ONE global store; identity comes from meta
    d = S.global_traj_dir()
    d.mkdir(parents=True, exist_ok=True)
    meta = {"model": "deepseek-v4-pro", "project_id": project_id,
            "project_name": project_name, "workdir": str(workdir)}
    if bench:
        meta.update({"runner": "rockycode", "instance_id": "x__y-1"})
    elif goal:
        # goal runs carry project_id FOR THE DREAM (graded with the project's
        # chats) but must stay out of the resume picker.
        meta.update({"runner": "goal", "goal": user_msg})
    else:
        meta["source"] = "chat"
    lines = [
        {"t": t, "kind": "meta", "data": meta},
        {"t": t, "kind": "message", "data": {"role": "system", "content": "sys"}},
        {"t": t, "kind": "message", "data": {"role": "user", "content": user_msg}},
        {"t": t, "kind": "message", "data": {"role": "assistant", "content": "ok"}},
    ]
    f = d / f"{sid}.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return f


def main():
    projA = Path(tempfile.mkdtemp(prefix="projA-"))
    projB = Path(tempfile.mkdtemp(prefix="projB-"))

    pa = S.get_project(projA)
    pb = S.get_project(projB)
    assert pa.id != pb.id
    assert (projA / S.PROJECT_REL).exists(), "project.json not created"

    # sessions in both projects + a bench session that must be hidden
    _make_session(pa.id, pa.name, projA, "20260101-aaa", "fix the dpi bug", time.time() - 100)
    _make_session(pa.id, pa.name, projA, "20260101-bbb", "explain the repo", time.time() - 10)  # newest in A
    _make_session(pb.id, pb.name, projB, "20260101-ccc", "add web search", time.time() - 50)
    _make_session(pa.id, pa.name, projA, "20260101-ddd", "BENCH should be hidden", time.time(), bench=True)
    _make_session(pa.id, pa.name, projA, "20260101-eee", "GOAL should be hidden", time.time(), goal=True)

    # scope=project: only A's chat sessions, newest first
    a = S.list_sessions("project", workdir=projA)
    assert [i.summary for i in a] == ["explain the repo", "fix the dpi bug"], a
    assert all("BENCH" not in i.summary for i in a), "bench session leaked into picker"
    assert all("GOAL" not in i.summary for i in a), "goal session leaked into picker"

    # scope=all: across both registered folders
    alls = S.list_sessions("all")
    summaries = [i.summary for i in alls]
    assert "add web search" in summaries and "explain the repo" in summaries, summaries
    assert alls[0].started_at >= alls[-1].started_at, "not sorted newest-first"

    # search
    found = S.list_sessions("all", query="web")
    assert len(found) == 1 and found[0].summary == "add web search", found

    # rename survival: move projA to a new path, relaunch get_project there
    moved = Path(str(projA) + "-renamed")
    shutil.move(str(projA), str(moved))
    pa2 = S.get_project(moved)
    assert pa2.id == pa.id, "project id changed on rename!"
    a2 = S.list_sessions("project", workdir=moved)
    assert len(a2) == 2, "sessions lost after rename"
    # registry 'current' now points at the new path
    reg = json.loads(S.REGISTRY.read_text())
    assert reg[pa.id]["current"] == str(moved.resolve()), reg[pa.id]

    # load_history reconstructs the message list (no meta/usage lines)
    hist = S.load_history(a2[0].path)
    assert [m["role"] for m in hist] == ["system", "user", "assistant"], hist

    print("projects:", pa.id[:8], pb.id[:8], "| all sessions:", len(alls))
    print("SESSION SMOKE OK — survives rename, cross-folder, search, load. amaze!")


main()
