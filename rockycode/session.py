"""Session storage: stable project identity + cross-folder discovery.

Claude Code / Codex key sessions by a folder's absolute path in a global
store, so renaming or moving the folder ORPHANS its sessions. rockycode does
it differently:

- Each project gets a STABLE id in `.rockycode/project.json`. The file lives
  in the folder, so it travels on rename/move — the project keeps its identity
  no matter where it is or what it's called.
- Trajectories live in ONE global store (`~/.rockycode/trajectories`); each
  file's meta records which project it belongs to, so sessions follow the
  project identity, not the folder path.
- A tiny global registry (`~/.rockycode/projects.json`) records where each
  project currently lives, so sessions are discoverable ACROSS folders — the
  resume picker can list "this folder" or "all folders", and `--resume <id>`
  can land back in a project's CURRENT folder even after a rename.

So: sessions survive folder renames AND are searchable across the machine —
which neither Claude Code nor Codex manage.

Public session ids are `rk_<hash>` — the uuid tail of the trajectory stem
(filenames keep their sortable timestamp form on disk; humans get the short
hash, like opencode's `ses_…`).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_HOME_ENV = os.environ.get("ROCKYCODE_HOME")
HOME_ROOT = Path(_HOME_ENV).expanduser() if _HOME_ENV else Path.home() / ".rockycode"
REGISTRY = HOME_ROOT / "projects.json"
PROJECT_REL = Path(".rockycode") / "project.json"
TRAJ_REL = Path(".rockycode") / "trajectories"


@dataclass
class Project:
    id: str
    name: str
    root: Path


@dataclass
class SessionInfo:
    session_id: str
    path: Path
    project_id: str
    project_name: str
    project_path: str
    model: str
    started_at: float
    n_messages: int
    summary: str
    title: str = ""  # flash-generated; empty on old/offline sessions

    @property
    def display_title(self) -> str:
        return self.title or self.summary


# ---- project identity + registry -------------------------------------------

def get_project(workdir: Path) -> Project:
    """Stable identity for a folder. Creates `.rockycode/project.json` on first
    use; that file travels with the folder, so the id survives renames."""
    workdir = Path(workdir).resolve()
    pf = workdir / PROJECT_REL
    if pf.exists():
        try:
            data = json.loads(pf.read_text(encoding="utf-8"))
            proj = Project(id=data["project_id"], name=data.get("name", workdir.name), root=workdir)
        except (json.JSONDecodeError, OSError, KeyError):
            proj = _new_project(workdir, pf)
    else:
        proj = _new_project(workdir, pf)
    _register(proj)
    return proj


def _new_project(workdir: Path, pf: Path) -> Project:
    proj = Project(id=uuid.uuid4().hex, name=workdir.name, root=workdir)
    try:
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(
            json.dumps({"project_id": proj.id, "name": proj.name, "created": time.time()}, indent=2),
            encoding="utf-8",
        )
    except OSError:
        # Read-only / unwritable cwd: use an ephemeral id for this run instead of
        # crashing chat at startup. It just won't persist across launches (so
        # resume-by-project won't find it), which is the right degradation.
        pass
    return proj


def _load_registry() -> dict:
    if REGISTRY.exists():
        try:
            return json.loads(REGISTRY.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _register(proj: Project) -> None:
    """Record (and self-heal) where this project lives. `current` is the
    authoritative path; old paths linger but are harmless. Best-effort: the
    registry only powers cross-folder resume, so an unwritable home must never
    block startup."""
    try:
        HOME_ROOT.mkdir(parents=True, exist_ok=True)
        reg = _load_registry()
        entry = reg.get(proj.id, {})
        paths = set(entry.get("paths", []))
        paths.add(str(proj.root))
        reg[proj.id] = {
            "name": proj.name,
            "current": str(proj.root),
            "paths": sorted(paths),
            "last_seen": time.time(),
        }
        REGISTRY.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    except OSError:
        pass


# ---- session discovery -----------------------------------------------------

def _is_chat_session(meta: dict) -> bool:
    # bench, goal, and routine runs land in the same trajectories dir but
    # carry a runner / instance_id; the resume picker only wants interactive
    # chat sessions. (Goal/routine runs DO carry project_id now — that's for
    # the dream, which grades them alongside chats, not for the picker.)
    return "instance_id" not in meta and meta.get("runner") not in ("rockycode", "goal", "routine")


def global_traj_dir() -> Path:
    """The single global trajectory store. Read at call time so a test that
    redirects HOME_ROOT takes effect."""
    return HOME_ROOT / "trajectories"


def _read_info(traj: Path) -> Optional[SessionInfo]:
    """Build a SessionInfo from a trajectory; project identity comes from its
    own meta (project_id/project_name/workdir), since all sessions share the
    one global dir now."""
    try:
        lines = traj.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if not lines:
        return None
    meta: dict = {}
    summary = ""
    title = ""
    n_messages = 0
    started_at = traj.stat().st_mtime
    for ln in lines:
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        kind, data = rec.get("kind"), rec.get("data", {})
        if kind == "meta":
            meta = data
            started_at = rec.get("t", started_at)
        elif kind == "title":
            # last one wins — the trajectory is append-only, so regenerating a
            # title later just appends a fresher record
            t = data.get("title")
            if isinstance(t, str) and t.strip():
                title = t.strip()
        elif kind == "message":
            n_messages += 1
            if not summary and data.get("role") == "user":
                c = data.get("content")
                if isinstance(c, str) and c.strip():
                    summary = c.strip().splitlines()[0][:80]
    if not _is_chat_session(meta):
        return None
    workdir = meta.get("workdir", "")
    return SessionInfo(
        session_id=traj.stem,
        path=traj,
        project_id=meta.get("project_id", ""),
        project_name=meta.get("project_name") or (Path(workdir).name if workdir else "?"),
        project_path=workdir,
        model=meta.get("model", "?"),
        started_at=started_at,
        n_messages=n_messages,
        summary=summary or "(no message)",
        title=title,
    )


def list_sessions(
    scope: str = "project",
    *,
    workdir: Optional[Path] = None,
    query: Optional[str] = None,
    limit: int = 50,
) -> list[SessionInfo]:
    """Chat sessions from the global store, newest first. scope='project'
    keeps only this workdir's project (by project_id); scope='all' keeps every
    project. query filters by summary/project-name substring."""
    traj_dir = global_traj_dir()
    if not traj_dir.is_dir():
        return []
    target_pid: Optional[str] = None
    if scope != "all" and workdir is not None:
        target_pid = get_project(workdir).id
    infos: list[SessionInfo] = []
    for f in traj_dir.glob("*.jsonl"):
        info = _read_info(f)
        if info is None:
            continue
        if target_pid is not None and info.project_id != target_pid:
            continue
        infos.append(info)
    if query:
        q = query.lower()
        infos = [i for i in infos
                 if q in i.title.lower() or q in i.summary.lower() or q in i.project_name.lower()]
    infos.sort(key=lambda i: i.started_at, reverse=True)
    return infos[:limit]


# ---- public session ids ------------------------------------------------------

ID_PREFIX = "rk_"


def public_id(session_id: str) -> str:
    """`rk_ab12cd34` — the uuid tail of the trajectory stem, rocky-prefixed.
    The filename keeps its sortable stamp form on disk; humans get the hash."""
    return ID_PREFIX + session_id.rsplit("-", 1)[-1]


def resolve_session(token: str) -> tuple[Optional[SessionInfo], str]:
    """One session from a user-typed id. Accepted forms: `rk_ab12cd34`, bare
    `ab12cd34`, a unique hash prefix (≥4 chars), or a full legacy stem.
    Returns (info, error) — exactly one is set."""
    t = token.strip().lower()
    if t.startswith(ID_PREFIX):
        t = t[len(ID_PREFIX):]
    if not t:
        return None, "empty session id"
    sessions = list_sessions(scope="all", limit=100_000)
    exact = [s for s in sessions if s.session_id.lower() == t]
    if exact:
        return exact[0], ""
    if len(t) >= 4:
        hits = [s for s in sessions
                if s.session_id.rsplit("-", 1)[-1].lower().startswith(t)]
        if len(hits) == 1:
            return hits[0], ""
        if len(hits) > 1:
            opts = ", ".join(f"{public_id(s.session_id)} ({s.display_title[:30]})" for s in hits[:5])
            return None, f"'{token}' is ambiguous — matches {opts}"
    return None, f"no session matches '{token}' — run `rockycode --resume` to browse"


def project_current_path(project_id: str) -> Optional[Path]:
    """Where a project lives NOW, per the registry — survives folder renames.
    None when the registry has no entry (or the recorded path is gone)."""
    entry = _load_registry().get(project_id) or {}
    cur = entry.get("current")
    if cur and Path(cur).is_dir():
        return Path(cur)
    for p in reversed(entry.get("paths", [])):
        if Path(p).is_dir():
            return Path(p)
    return None


def load_history(traj: Path) -> list[dict]:
    """Reconstruct an engine message history from a trajectory file — exactly
    the {role, content, tool_calls, tool_call_id} dicts that were sent to the
    API (reasoning_content was never stored, so this is clean to replay)."""
    history: list[dict] = []
    try:
        lines = Path(traj).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return history
    for ln in lines:
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if rec.get("kind") == "message":
            history.append(rec["data"])
    return history
