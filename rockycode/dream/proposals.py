"""The proposals inbox (self-evolve phase 1, slice 4) — dream's only actuator.

Memory writes are free; anything EXECUTABLE is proposal-only. The dream may
draft a skill it thinks the project needs — from hot (reinforced) weaknesses
and this pass's episodes — but nothing self-installs: drafts land in a global
pending inbox and the user approves or archives each from the /proposals card
in the TUI. Approve installs a SKILL.md into the global ~/.rockycode/skills
(project skills still win discovery); archive keeps the file for provenance —
nothing is deleted, same rule as memory.

Drafting runs on LOCAL Ollama (episode text may carry exit-sheet-derived
content, which never goes to a cloud model) and is deliberately scarce:
at most ONE draft per dream pass, and none while the project already has
MAX_PENDING_PER_PROJECT waiting — an inbox that piles up is one that gets
ignored.

Proposal files carry `evidence:` as rk_ PUBLIC session ids (the stable
human-facing handle; resolve_session maps them back).
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rockycode.engine.skills import _parse_frontmatter

PENDING, APPROVED, ARCHIVED = "pending", "approved", "archived"
MAX_PENDING_PER_PROJECT = 3
HOT_IMPORTANCE = 7  # a weakness reinforced at least once

PROPOSE_PROMPT = """\
You draft SKILL playbooks for a coding agent — but ONLY when the evidence
truly supports one. A skill is a reusable, step-by-step procedure the agent
should follow for a recurring kind of task in this project.

RECURRING WEAKNESSES (reinforced across sessions):
{weaknesses}

THIS PASS'S SESSION NOTES:
{episodes}

SKILLS THAT ALREADY EXIST (never re-draft these):
{existing}

The weaknesses above are already RECURRING — each was observed across
multiple sessions. If a step-by-step checklist would prevent one of them,
draft that skill now. With no weaknesses listed, draft only when the session
notes themselves show a recurring procedure worth packaging. If neither
holds, reply with exactly: null

Reply with ONLY a JSON object (or the word null):
{{"name": "short-kebab-case-name",
  "description": "one line: when the agent should reach for this",
  "when_to_use": "one or two sentences",
  "steps": "markdown bullet list of concrete steps"}}
"""


def _home_root() -> Path:
    base = os.environ.get("ROCKYCODE_HOME")
    return Path(base).expanduser() if base else Path.home() / ".rockycode"


def proposals_dir() -> Path:
    """Global inbox, $ROCKYCODE_HOME-aware like trajectories. Proposal meta
    carries project_id, so the TUI filters per project."""
    return _home_root() / "proposals"


def skills_home() -> Path:
    """Global install target for approved skills (in skill discovery order
    after the project dirs — a project skill always wins)."""
    return _home_root() / "skills"


@dataclass
class Proposal:
    name: str
    kind: str = "skill"            # skill | (later: routine, prompt-edit)
    description: str = ""
    status: str = PENDING
    origin: str = "dream"
    created: str = ""
    project_id: str = ""
    project_name: str = ""
    reason: str = ""               # why dream drafted it (weakness/pattern)
    evidence: list[str] = field(default_factory=list)  # rk_ public session ids
    body: str = ""                 # the drafted SKILL.md body
    path: Optional[Path] = None


def _slugify(text: str, max_len: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "proposal"


def to_markdown(p: Proposal) -> str:
    return "\n".join([
        "---",
        f"name: {p.name}",
        f"kind: {p.kind}",
        f"description: {p.description}",
        f"status: {p.status}",
        f"origin: {p.origin}",
        f"created: {p.created}",
        f"project_id: {p.project_id}",
        f"project_name: {p.project_name}",
        f"reason: {p.reason}",
        f"evidence: [{', '.join(p.evidence)}]",
        "---",
        "",
        p.body.strip(),
        "",
    ])


def parse_proposal(text: str, path: Optional[Path] = None) -> Proposal:
    fields, body = _parse_frontmatter(text)
    ev = [v.strip().strip("'\"") for v in fields.get("evidence", "").strip("[]").split(",")
          if v.strip().strip("'\"")]
    return Proposal(
        name=fields.get("name") or (path.stem if path else "proposal"),
        kind=fields.get("kind", "skill"),
        description=fields.get("description", ""),
        status=fields.get("status", PENDING),
        origin=fields.get("origin", "dream"),
        created=fields.get("created", ""),
        project_id=fields.get("project_id", ""),
        project_name=fields.get("project_name", ""),
        reason=fields.get("reason", ""),
        evidence=ev,
        body=body.strip(),
        path=path,
    )


class ProposalStore:
    """pending/ approved/ archived/ under one global root — status IS the
    directory, so `ls` tells the truth and nothing needs a database."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = root or proposals_dir()

    def list(self, status: str = PENDING, project_id: Optional[str] = None) -> list[Proposal]:
        folder = self.root / status
        if not folder.is_dir():
            return []
        out = []
        for f in sorted(folder.glob("*.md")):
            try:
                p = parse_proposal(f.read_text(encoding="utf-8", errors="replace"), path=f)
            except OSError:
                continue
            if project_id is None or p.project_id == project_id:
                out.append(p)
        return out

    def all_names(self) -> set[str]:
        """Every proposal name in any status — the dedup set for drafting."""
        names = set()
        for status in (PENDING, APPROVED, ARCHIVED):
            folder = self.root / status
            if folder.is_dir():
                names.update(f.stem for f in folder.glob("*.md"))
        return names

    def save(self, p: Proposal) -> Path:
        if not p.created:
            p.created = time.strftime("%Y-%m-%d")
        folder = self.root / p.status
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{_slugify(p.name)}.md"
        path.write_text(to_markdown(p), encoding="utf-8")
        p.path = path
        return path

    def _move(self, p: Proposal, status: str) -> Path:
        old = p.path
        p.status = status
        new_path = self.save(p)
        if old is not None and old != new_path:
            try:
                old.unlink()
            except OSError:
                pass
        return new_path

    def approve(self, p: Proposal) -> Path:
        """Install the drafted proposal by KIND and file it under approved/ for
        provenance. A skill installs a SKILL.md; a routine installs a
        routine.toml (disabled-of-auto, no lease — the user grants the run
        envelope on the enable card, so nothing dream-drafted ever self-runs).
        Returns the installed path."""
        if p.kind == "routine":
            return self._approve_routine(p)
        name = _slugify(p.name)
        target = skills_home() / name
        while (target / "SKILL.md").exists():
            name += "-dream"
            target = skills_home() / name
        target.mkdir(parents=True, exist_ok=True)
        skill_md = "\n".join([
            "---",
            f"name: {name}",
            f"description: {p.description}",
            "origin: dream",
            f"evidence: [{', '.join(p.evidence)}]",
            f"project: {p.project_name}",
            "---",
            "",
            p.body.strip(),
            "",
        ])
        installed = target / "SKILL.md"
        installed.write_text(skill_md, encoding="utf-8")
        self._move(p, APPROVED)
        return installed

    def _approve_routine(self, p: Proposal) -> Path:
        """Install a drafted routine.toml (p.body IS the toml). Installed
        enabled-but-click-to-run: auto=false and no lease, so it shows as a due
        card the user runs by hand until they explicitly grant the auto lease —
        a dream draft never earns unattended execution on its own."""
        from rockycode.routines import routines_dir
        name = _slugify(p.name)
        target = routines_dir() / name
        while (target / "routine.toml").exists():
            name += "-dream"
            target = routines_dir() / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "routine.toml").write_text(p.body.strip() + "\n", encoding="utf-8")
        self._move(p, APPROVED)
        return target / "routine.toml"

    def archive(self, p: Proposal) -> Path:
        return self._move(p, ARCHIVED)


def failing_routines(project_id: str, *, window: int = 4, min_fails: int = 2) -> list[dict]:
    """Routines whose recent runs keep failing — the signal for an amendment
    proposal. A run status is 'done' on success; anything else (blocked/budget/
    error) counts as a failure. Returns the routine + its recent failure count."""
    from rockycode.routines import RoutineStore
    store = RoutineStore()
    out = []
    for r in store.list(project_id=project_id):
        runs = store.state(r).runs[-window:]
        fails = [x for x in runs if x.get("status") != "done"]
        if len(runs) >= min_fails and len(fails) >= min_fails:
            out.append({"routine": r, "fails": len(fails), "of": len(runs),
                        "last_status": (runs[-1].get("status") if runs else "error")})
    return out


PROPOSE_ROUTINE_PROMPT = """\
You draft ROUTINES for a coding agent — a routine is a task the agent runs by
itself on a schedule (daily or weekly). Draft one ONLY when the evidence
genuinely supports it.

A FAILING ROUTINE that needs amending (fix its prompt/cadence/budget):
{failing}

THIS PASS'S SESSION NOTES (look for a recurring MANUAL task worth automating):
{episodes}

ROUTINES THAT ALREADY EXIST (never re-draft these):
{existing}

If a routine above is failing, draft an amendment (same name, a clearer prompt
or a smaller/larger cadence). Otherwise, if the notes show the user repeatedly
doing the same task by hand, draft a routine to do it for them. If neither
holds, reply with exactly: null

Reply with ONLY a JSON object (or the word null):
{{"name": "short-kebab-case-name",
  "description": "one line: what this routine does",
  "cadence": "daily" or "weekly",
  "prompt": "the exact task line the agent should run each time"}}
"""


def _parse_routine_draft(answer: str) -> Optional[dict]:
    if answer.strip().lower().startswith("null"):
        return None
    m = re.search(r"\{.*\}", answer, re.DOTALL)
    if m is None:
        return None
    try:
        obj = json.loads(m.group())
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("cadence") not in ("daily", "weekly"):
        obj["cadence"] = "daily"
    if not all(isinstance(obj.get(k), str) and obj[k].strip()
               for k in ("name", "description", "prompt")):
        return None
    return obj


async def draft_routine_proposals(runner, episode_summaries: list[str], session_ids: list[str]) -> None:
    """The dream's ROUTINE-proposal job (phase 2, slice 4): amend a failing
    routine, or propose automating a recurring manual task. Local Ollama only,
    at most one draft, inbox-capped. Mutates runner.report; respects dry_run."""
    from rockycode.routines import Routine, RoutineStore, _emit_toml
    from rockycode.session import get_project, public_id

    store = ProposalStore()
    project = get_project(runner.workdir)
    if len(store.list(PENDING, project_id=project.id)) >= MAX_PENDING_PER_PROJECT:
        return

    failing = failing_routines(project.id)
    if not failing and len(episode_summaries) < 2:
        return  # no failing routine and not enough recurrence to automate

    rstore = RoutineStore()
    existing = "\n".join(f"- {r.name}: {r.description}" for r in rstore.list(project.id)) or "(none)"
    taken = {r.name for r in rstore.list(project.id)} | store.all_names()
    fail_txt = "\n".join(
        f"- {f['routine'].name}: failed {f['fails']}/{f['of']} recent runs "
        f"(last: {f['last_status']}) — prompt was: {f['routine'].prompt[:160]}"
        for f in failing) or "(none)"

    runner.log("routine-proposal pass · drafting (local)")
    answer = await runner.chat.chat(PROPOSE_ROUTINE_PROMPT.format(
        failing=fail_txt,
        episodes="\n\n".join(episode_summaries)[:4000],
        existing=existing,
    ), max_tokens=1024)
    draft = _parse_routine_draft(answer)
    if draft is None:
        return
    name = _slugify(draft["name"])
    is_amendment = any(f["routine"].name == name for f in failing)
    if name in taken and not is_amendment:
        return  # a NEW routine can't reuse a name; an amendment reuses on purpose

    # Conservative envelope — the user grants the real one on the enable card.
    routine = Routine(
        name=name, description=draft["description"][:200], cadence=draft["cadence"],
        prompt=draft["prompt"], workdir=str(runner.workdir), project_id=project.id,
        network=False, isolation=False, budget_run=0.10, max_steps=30,
        auto=False, lease_deadline=0.0, enabled=True,
    )
    reason = (f"amend failing routine '{name}'" if is_amendment
              else "recurring manual task in episodes")
    proposal = Proposal(
        name=name, kind="routine", description=draft["description"][:200],
        project_id=project.id, project_name=project.name, reason=reason,
        evidence=[public_id(sid) for sid in session_ids],
        body=_emit_toml(routine),
    )
    if runner.dry_run:
        runner.log(f"routine proposal (dry-run, not saved): {name} · {reason}")
        return
    store.save(proposal)
    runner.report.proposals_drafted += 1
    runner.log(f"drafted routine proposal: {name} — /proposals to review · {reason}")


def _parse_draft(answer: str) -> Optional[dict]:
    if answer.strip().lower().startswith("null"):
        return None
    m = re.search(r"\{.*\}", answer, re.DOTALL)
    if m is None:
        return None
    try:
        obj = json.loads(m.group())
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    # The prompt asks for a markdown-string `steps`, but stronger models
    # reasonably emit a JSON array (observed live with qwen3.5:9b) —
    # normalize a list of strings into bullets instead of voiding the draft.
    steps = obj.get("steps")
    if isinstance(steps, list) and steps and all(isinstance(s, str) for s in steps):
        obj["steps"] = "\n".join(f"- {s.strip()}" for s in steps)
    if not all(isinstance(obj.get(k), str) and obj[k].strip()
               for k in ("name", "description", "when_to_use", "steps")):
        return None
    return obj


async def draft_proposals(runner, episode_summaries: list[str], session_ids: list[str]) -> None:
    """The dream's proposal job — gate first, one local call, at most one
    draft. Mutates runner.report; respects runner.dry_run."""
    from rockycode.engine.skills import discover_skills
    from rockycode.session import get_project, public_id

    store = ProposalStore()
    project = get_project(runner.workdir)
    if len(store.list(PENDING, project_id=project.id)) >= MAX_PENDING_PER_PROJECT:
        return  # the inbox is full — earn attention before asking for more
    hot = [m for m in runner.store.load_all()
           if m.type == "weakness" and m.importance >= HOT_IMPORTANCE]
    if not hot and len(episode_summaries) < 2:
        return  # not enough recurrence to package anything

    skills = discover_skills(runner.workdir, home=Path.home())
    existing = "\n".join(f"- {s.name}: {s.description}" for s in skills) or "(none)"
    taken = {s.name for s in skills} | store.all_names()

    runner.log("proposal pass · drafting (local)")
    answer = await runner.chat.chat(PROPOSE_PROMPT.format(
        weaknesses="\n".join(f"- [{m.name}] {m.description} — {m.body[:200]}" for m in hot) or "(none)",
        episodes="\n\n".join(episode_summaries)[:4000],
        existing=existing,
    ), max_tokens=1536)
    draft = _parse_draft(answer)
    if draft is None:
        return
    name = _slugify(draft["name"])
    if name in taken:
        return  # already exists or already proposed — recurrence isn't novelty

    body = (f"# {draft['name']}\n\n{draft['description']}\n\n"
            f"## when to use\n{draft['when_to_use']}\n\n"
            f"## steps\n{draft['steps']}")
    reason = f"hot weakness: {hot[0].name}" if hot else "recurring pattern in episodes"
    proposal = Proposal(
        name=name,
        description=draft["description"][:200],
        project_id=project.id,
        project_name=project.name,
        reason=reason,
        evidence=[public_id(sid) for sid in session_ids],
        body=body,
    )
    runner.report.proposals_drafted += 1
    runner.report.decisions.append(f"PROPOSAL +{name}: {proposal.description[:80]}")
    if not runner.dry_run:
        store.save(proposal)
