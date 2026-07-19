"""Isolated workspace for a goal run: a git worktree on a fresh branch, so the
agent works on a COPY of the repo. Nothing it does — even a destructive command
that slips past the safety classifier — touches the user's working tree. The
morning review is `git diff` / merging the goal branch.

Falls back to a plain directory copy when the target isn't a git repo, so goal
mode still isolates the real files, just without the nice diff/merge.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


# Build/download detritus a goal milestone can drop into the workspace (a pip
# bootstrap in a slim sandbox pulls .deb files + a venv; npm pulls node_modules).
# Excluded from the commit so the review branch stays reviewable — the actual
# source changes aren't buried under megabytes of binaries.
_COMMIT_EXCLUDE = [
    ":(exclude)*.deb", ":(exclude)*.whl", ":(exclude)*.pyc", ":(exclude)*.egg-info",
    ":(exclude)venv/", ":(exclude).venv/", ":(exclude)env/",
    ":(exclude)node_modules/", ":(exclude)__pycache__/",
    ":(exclude)dist/", ":(exclude)build/", ":(exclude).mypy_cache/", ":(exclude).pytest_cache/",
]


def _is_git_repo(path: Path) -> bool:
    return _git(path, "rev-parse", "--is-inside-work-tree").returncode == 0


@dataclass
class GoalWorkspace:
    path: Path                # where the goal agent works (the isolated copy)
    branch: Optional[str]     # goal branch (git case) or None (plain-copy case)
    origin: Path              # the user's real repo
    _worktree: bool
    base: Optional[str] = None  # fork-point commit SHA (git case), for diff/review

    @classmethod
    def create(cls, repo: Path, slug: str) -> "GoalWorkspace":
        repo = Path(repo).resolve()
        dest = repo.parent / f".rockycode-goal-{slug}"
        if dest.exists():
            raise FileExistsError(f"goal workspace already exists: {dest}")
        if _is_git_repo(repo):
            base = _git(repo, "rev-parse", "HEAD").stdout.strip() or "HEAD"
            branch = f"goal/{slug}"
            r = _git(repo, "worktree", "add", "-b", branch, str(dest), "HEAD")
            if r.returncode != 0:
                raise RuntimeError(f"git worktree add failed: {r.stderr.strip()}")
            return cls(dest, branch, repo, True, base)
        # not a git repo → plain copy (still isolates the real files)
        shutil.copytree(
            repo, dest,
            ignore=shutil.ignore_patterns(".git", "node_modules", ".venv", "__pycache__"),
        )
        return cls(dest, None, repo, False, None)

    def commit(self, message: str) -> bool:
        """Checkpoint the current worktree state onto the goal branch. Host-side
        and scoped to THIS worktree; no-op for a plain-copy workspace or when
        nothing changed. Returns True iff a commit was made.

        Refuses to run unless the worktree's HEAD is the goal branch — a paranoia
        guard so a goal run can NEVER advance the user's own branch (worktree
        semantics already guarantee this; the check makes a surprise fail loud)."""
        if not self._worktree:
            return False
        head = _git(self.path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if head != self.branch:
            raise RuntimeError(
                f"refusing to commit: {self.path} is on '{head}', not the goal "
                f"branch '{self.branch}'")
        _git(self.path, "add", "-A", "--", ".", *_COMMIT_EXCLUDE)
        if _git(self.path, "diff", "--cached", "--quiet").returncode == 0:
            return False  # nothing changed this milestone — no empty commit
        # Inline identity: an unattended env may have no global git user set.
        r = _git(self.path, "-c", "user.name=rockycode",
                 "-c", "user.email=goal@rockycode.local", "commit", "-m", message)
        return r.returncode == 0

    def diff(self) -> str:
        """The full change the goal made (committed + uncommitted), for review."""
        if self._worktree:
            return _git(self.path, "diff", self.base or "HEAD").stdout
        return "[copy workspace — no git diff; compare the directory manually]"

    def cleanup(self, *, keep: bool = True) -> None:
        """keep=True (default): leave the branch/worktree so you can review and
        merge in the morning. keep=False: remove the worktree (the branch stays,
        in the git case, so committed work survives)."""
        if keep:
            return
        if self._worktree:
            _git(self.origin, "worktree", "remove", "--force", str(self.path))
        elif self.path.exists():
            shutil.rmtree(self.path, ignore_errors=True)


def prune_goal_worktrees(repo: Path) -> list[str]:
    """Remove leftover goal worktrees (`.rockycode-goal-*`) registered on *repo*
    — they accumulate one per run. Branches are KEPT (committed work survives on
    `goal/<slug>`, reviewable/mergeable). Returns the paths removed. Also drops
    plain-copy leftovers next to the repo. Best-effort; never raises."""
    repo = Path(repo).resolve()
    removed: list[str] = []
    if _is_git_repo(repo):
        for line in _git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
            if not line.startswith("worktree "):
                continue
            path = line[len("worktree "):].strip()
            if Path(path).name.startswith(".rockycode-goal-"):
                if _git(repo, "worktree", "remove", "--force", path).returncode == 0:
                    removed.append(path)
        _git(repo, "worktree", "prune")  # clear stale admin entries
    # plain-copy fallback workspaces (non-git runs) live beside the repo too
    for d in repo.parent.glob(".rockycode-goal-*"):
        if d.is_dir() and str(d) not in removed:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(str(d))
    return removed
