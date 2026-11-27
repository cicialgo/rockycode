"""The proposals inbox — dream's only actuator (evolve v2).

Memory writes are free; anything EXECUTABLE is proposal-only. The dream may
draft a skill or a routine — from hot (reinforced) weaknesses and habits and
this pass's episodes — but nothing self-installs: drafts land in a global
pending inbox and the user approves or archives each from the /proposals card
in the TUI. Approve installs by kind; archive keeps the file for provenance
AND decays the pattern behind it (a "no" is a signal too — see
patterns.decay_pattern). Nothing is deleted, same rule as memory.

Every draft passes the QUALITY GATE before the inbox ever sees it (locked
2026-08-02): mechanical lint (frontmatter/toml must round-trip), one critique
call (is the draft actually supported by the evidence?), and — for routines —
one sandboxed DRY-RUN whose receipt rides the card ("tested ✓ · $0.03").
A draft that fails its own test never asks for the user's attention; the
pattern stays hot and a better draft gets its chance next pass.

Drafting stays deliberately scarce: at most ONE draft of each kind per dream
pass, none while the project already has MAX_PENDING_PER_PROJECT waiting —
an inbox that piles up is one that gets ignored.

Proposal files carry `evidence:` as rk_ PUBLIC session ids (the stable
human-facing handle; resolve_session maps them back) and `pattern:` as the
memory-store name of the hot pattern that drafted them (the decay handle).
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
HOT_IMPORTANCE = 7  # a pattern reinforced at least once (cross-session)

CRITIQUE_PROMPT = """\
You are the quality gate for a coding agent's self-drafted {kind} proposal.
Judge STRICTLY whether this draft is (1) actually supported by the evidence,
(2) concrete enough to execute as written, and (3) not a rewording of
something listed as already existing. Reject speculation and vagueness — a
wrong proposal costs the user trust; a missing one costs nothing, the
pattern will recur.

EVIDENCE THE DRAFT CLAIMS TO REST ON:
{evidence}

THE DRAFT:
{draft}

Reply with ONLY a JSON object:
{{"verdict": "ok" or "reject", "reason": "one line"}}
"""

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
    kind: str = "skill"            # skill | routine | (later: prompt-edit)
    description: str = ""
    status: str = PENDING
    origin: str = "dream"
    created: str = ""
    project_id: str = ""
    project_name: str = ""
    reason: str = ""               # why dream drafted it (weakness/pattern)
    pattern: str = ""              # memory-store name of the hot pattern (decay handle)
    amends: str = ""               # routine name this draft REPLACES (amendments only)
    tested: str = ""               # dry-run receipt: ok | failed | unavailable | "" (skill)
    test_note: str = ""            # human line for the card ("ran clean · 8 steps · $0.03")
    evidence: list[str] = field(default_factory=list)  # rk_ public session ids
    body: str = ""                 # the drafted SKILL.md body / routine.toml
    path: Optional[Path] = None


def _slugify(text: str, max_len: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "proposal"


def _fm(value: str) -> str:
    """One frontmatter value, made single-line: the lenient parser reads
    `key: value` per line, so a model-drafted newline would silently truncate
    the field (and could smuggle a fake key) — collapse, don't trust."""
    return re.sub(r"\s+", " ", str(value)).strip()


def to_markdown(p: Proposal) -> str:
    return "\n".join([
        "---",
        f"name: {_fm(p.name)}",
        f"kind: {_fm(p.kind)}",
        f"description: {_fm(p.description)}",
        f"status: {_fm(p.status)}",
        f"origin: {_fm(p.origin)}",
        f"created: {_fm(p.created)}",
        f"project_id: {_fm(p.project_id)}",
        f"project_name: {_fm(p.project_name)}",
        f"reason: {_fm(p.reason)}",
        f"pattern: {_fm(p.pattern)}",
        f"amends: {_fm(p.amends)}",
        f"tested: {_fm(p.tested)}",
        f"test_note: {_fm(p.test_note)}",
        f"evidence: [{', '.join(_fm(e) for e in p.evidence)}]",
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
        pattern=fields.get("pattern", ""),
        amends=fields.get("amends", ""),
        tested=fields.get("tested", ""),
        test_note=fields.get("test_note", ""),
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
        if p.kind == "retire-skill":
            return self._approve_retire(p)
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
        a dream draft never earns unattended execution on its own.

        An AMENDMENT (p.amends) REPLACES the failing routine in place — the
        old toml is kept beside it as routine.toml.prev and the state.json
        run history stays, so the watch loop sees the fix take effect. (The
        old collision behavior installed a `-dream` sibling while the broken
        original kept failing — the two paths contradicted each other.)"""
        from rockycode.routines import routines_dir
        name = _slugify(p.amends or p.name)
        target = routines_dir() / name
        if p.amends and (target / "routine.toml").exists():
            try:
                os.replace(target / "routine.toml", target / "routine.toml.prev")
            except OSError:
                pass
        else:
            while (target / "routine.toml").exists():
                name += "-dream"
                target = routines_dir() / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "routine.toml").write_text(p.body.strip() + "\n", encoding="utf-8")
        self._move(p, APPROVED)
        return target / "routine.toml"

    def _approve_retire(self, p: Proposal) -> Path:
        """Retire an installed dream skill: MOVE its directory into
        skills/.archived (archive-not-delete — a dotted dir is invisible to
        discovery, and moving it back reinstalls). p.pattern names the skill."""
        name = _slugify(p.pattern or p.name.removeprefix("retire-"))
        src = skills_home() / name
        dst_root = skills_home() / ".archived"
        dst = dst_root / name
        if src.is_dir():
            dst_root.mkdir(parents=True, exist_ok=True)
            n = 1
            while dst.exists():
                dst = dst_root / f"{name}-{n}"
                n += 1
            os.replace(src, dst)
        self._move(p, APPROVED)
        return dst

    def archive(self, p: Proposal) -> Path:
        return self._move(p, ARCHIVED)


RETIRE_AFTER_SESSIONS = 5  # dream skill never loaded across this many later sessions


def watch_skills(runner) -> None:
    """The skill watch (evolve v2, slice 4) — pure code, no model call. A
    dream-origin skill that NO session has loaded (usage sidecar, see
    engine/skills.bump_skill_usage) across RETIRE_AFTER_SESSIONS consolidated
    sessions since its install draws a retire proposal: same inbox, same
    human verdict, archive-not-delete (approve moves the skill directory to
    skills/.archived — moving it back reinstalls).

    Only skills the dream itself installed are ever watched — user-authored
    skills are structurally off-limits (the provenance boundary). Amending a
    used-but-unhelpful skill needs outcome correlation; deliberately later.
    """
    import time as _time

    from rockycode.engine.skills import _parse_frontmatter, read_skill_usage

    store = ProposalStore()
    from rockycode.session import get_project
    project = get_project(runner.workdir)
    if len(store.list(PENDING, project_id=project.id)) >= MAX_PENDING_PER_PROJECT:
        return
    usage = read_skill_usage()
    taken = store.all_names()
    episodes = [m for m in runner.store.load_all() if m.type == "episode"]

    root = skills_home()
    if not root.is_dir():
        return
    for d in sorted(root.iterdir()):
        md = d / "SKILL.md"
        if not (d.is_dir() and md.exists()):
            continue
        try:
            fields, body = _parse_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if fields.get("origin") != "dream":
            continue  # never touch what the dream didn't write
        name = fields.get("name") or d.name
        if _slugify(f"retire-{name}") in taken:
            continue  # already proposed once (any status) — never nag
        entry = usage.get(name)
        if isinstance(entry, dict) and int(entry.get("loads", 0)) > 0:
            continue  # it earns its keep
        # Sessions since install: session ids start YYYYMMDD-HHMMSS, so the
        # install mtime rendered as a stamp compares lexicographically.
        stamp = _time.strftime("%Y%m%d-%H%M%S", _time.localtime(md.stat().st_mtime))
        seen = sum(1 for m in episodes if any(sid > stamp for sid in m.evidence))
        if seen < RETIRE_AFTER_SESSIONS:
            continue
        proposal = Proposal(
            name=f"retire-{name}", kind="retire-skill",
            description=f"retire '{name}' — never loaded in {seen} sessions since install",
            project_id=project.id, project_name=project.name,
            reason=f"dream installed it, {seen} sessions later nothing has loaded it",
            pattern=name,
            body=body.strip() or f"# {name}\n\n(the installed skill)",
        )
        runner.report.proposals_drafted += 1
        runner.report.decisions.append(f"PROPOSAL +retire-{name}: unused dream skill")
        if not runner.dry_run:
            store.save(proposal)
        runner.log(f"watch: '{name}' never loaded in {seen} sessions — retire proposed")
        return  # at most one retire ask per pass — the inbox stays calm


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

RECURRING MANUAL HABITS (the user keeps doing these by hand, seen across sessions):
{habits}

THIS PASS'S SESSION NOTES:
{episodes}

ROUTINES THAT ALREADY EXIST (never re-draft these):
{existing}

If a routine above is failing, draft an amendment (same name, a clearer prompt
or a smaller/larger cadence). Otherwise, if a habit or the notes show the user
repeatedly doing the same task by hand, draft a routine to do it for them.
If neither holds, reply with exactly: null

Reply with ONLY a JSON object (or the word null):
{{"name": "short-kebab-case-name",
  "description": "one line: what this routine does",
  "cadence": "daily" or "weekly",
  "network": true only if the task NEEDS internet access, else false,
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
    obj["network"] = obj.get("network") is True
    if not all(isinstance(obj.get(k), str) and obj[k].strip()
               for k in ("name", "description", "prompt")):
        return None
    return obj


async def _critique(runner, kind: str, draft_text: str, evidence: str) -> tuple[bool, str]:
    """The gate's judgment layer: one call, strict by construction. An
    unparseable verdict REJECTS — precision first; the pattern stays hot and
    a better draft gets its chance next pass."""
    answer = await runner.chat.chat(CRITIQUE_PROMPT.format(
        kind=kind, evidence=evidence[:3000], draft=draft_text[:2500]), max_tokens=256)
    m = re.search(r"\{.*\}", answer or "", re.DOTALL)
    if m is None:
        return False, "critique unparseable"
    try:
        obj = json.loads(m.group())
    except json.JSONDecodeError:
        return False, "critique unparseable"
    verdict = str(obj.get("verdict", "")).strip().lower() if isinstance(obj, dict) else ""
    reason = str(obj.get("reason", "")).strip()[:200] if isinstance(obj, dict) else ""
    return verdict == "ok", reason or "no reason given"


async def draft_routine_proposals(runner, episode_summaries: list[str], session_ids: list[str]) -> None:
    """The dream's ROUTINE-proposal job: amend a failing routine, or automate
    a hot HABIT / recurring manual task. At most one draft, inbox-capped, and
    gated — lint, critique, sandboxed dry-run — before the inbox ever sees it.
    Mutates runner.report; respects dry_run."""
    import tomllib

    from rockycode.routines import Routine, RoutineStore, _emit_toml, dry_run_routine
    from rockycode.session import get_project, public_id

    store = ProposalStore()
    project = get_project(runner.workdir)
    if len(store.list(PENDING, project_id=project.id)) >= MAX_PENDING_PER_PROJECT:
        return

    failing = failing_routines(project.id)
    hot_habits = [m for m in runner.store.load_all()
                  if m.type == "habit" and m.importance >= HOT_IMPORTANCE]
    if not failing and not hot_habits and len(episode_summaries) < 2:
        return  # nothing failing, no hot habit, not enough recurrence

    rstore = RoutineStore()
    existing = "\n".join(f"- {r.name}: {r.description}" for r in rstore.list(project.id)) or "(none)"
    taken = {r.name for r in rstore.list(project.id)} | store.all_names()
    fail_txt = "\n".join(
        f"- {f['routine'].name}: failed {f['fails']}/{f['of']} recent runs "
        f"(last: {f['last_status']}) — prompt was: {f['routine'].prompt[:160]}"
        for f in failing) or "(none)"
    habits_txt = "\n".join(f"- [{m.name}] {m.description} — {m.body[:200]}"
                           for m in hot_habits) or "(none)"
    evidence_txt = f"FAILING ROUTINES:\n{fail_txt}\n\nHABITS:\n{habits_txt}\n\nEPISODES:\n" \
                   + "\n\n".join(episode_summaries)[:2500]

    runner.log("routine-proposal pass · drafting")
    answer = await runner.chat.chat(PROPOSE_ROUTINE_PROMPT.format(
        failing=fail_txt,
        habits=habits_txt,
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

    # Conservative envelope — network only as drafted (the task may need it);
    # the user grants the real envelope on the enable card.
    routine = Routine(
        name=name, description=draft["description"][:200], cadence=draft["cadence"],
        prompt=draft["prompt"], workdir=str(runner.workdir), project_id=project.id,
        network=draft["network"], isolation=False, budget_run=0.10, max_steps=30,
        auto=False, lease_deadline=0.0, enabled=True,
    )

    # ── the quality gate ─────────────────────────────────────────────────────
    body = _emit_toml(routine)
    try:  # lint: the emitted toml must round-trip, or approve would install noise
        tomllib.loads(body)
    except tomllib.TOMLDecodeError as e:
        runner.report.decisions.append(f"GATE lint dropped routine '{name}': {e}")
        return
    ok, why = await _critique(runner, "routine", body, evidence_txt)
    if not ok:
        runner.report.decisions.append(f"GATE critique dropped routine '{name}': {why}")
        return
    # Dry-run: prove the draft once, sandboxed, before it asks for attention.
    # A dream --dry-run stays execution-free; a chat-fake (tests) has no client.
    client = getattr(runner.chat, "client", None)
    model = getattr(runner.chat, "model", "")
    if runner.dry_run or not getattr(runner, "test_routines", True) or client is None or not model:
        receipt = {"tested": "unavailable", "note": "not executed (preview)"}
    else:
        runner.log(f"routine-proposal pass · dry-running '{name}' (sandboxed)")
        receipt = await dry_run_routine(routine, model=model, client=client)
    if receipt["tested"] == "failed":
        runner.report.decisions.append(
            f"GATE dry-run dropped routine '{name}': {receipt['note']}")
        return  # a draft that fails its own test never reaches the inbox

    reason = (f"amend failing routine '{name}'" if is_amendment
              else (f"hot habit: {hot_habits[0].name}" if hot_habits
                    else "recurring manual task in episodes"))
    proposal = Proposal(
        name=name, kind="routine", description=draft["description"][:200],
        project_id=project.id, project_name=project.name, reason=reason,
        pattern=hot_habits[0].name if hot_habits and not is_amendment else "",
        amends=name if is_amendment else "",
        tested=receipt["tested"], test_note=receipt.get("note", ""),
        evidence=[public_id(sid) for sid in session_ids],
        body=body,
    )
    runner.report.proposals_drafted += 1
    runner.report.decisions.append(f"PROPOSAL +{name} (routine): {proposal.description[:80]}")
    if runner.dry_run:
        runner.log(f"routine proposal (dry-run, not saved): {name} · {reason}")
        return
    store.save(proposal)
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
    """The dream's SKILL-proposal job — gates first, one drafting call, at
    most one draft, then the quality gate (lint + critique; skills have
    nothing to execute, so no dry-run — the watch loop judges them after
    install). Mutates runner.report; respects runner.dry_run."""
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

    weaknesses_txt = "\n".join(f"- [{m.name}] {m.description} — {m.body[:200]}"
                               for m in hot) or "(none)"
    runner.log("proposal pass · drafting")
    answer = await runner.chat.chat(PROPOSE_PROMPT.format(
        weaknesses=weaknesses_txt,
        episodes="\n\n".join(episode_summaries)[:4000],
        existing=existing,
    ), max_tokens=1536)
    draft = _parse_draft(answer)
    if draft is None:
        return
    name = _slugify(draft["name"])
    if name in {_slugify(t) for t in taken}:
        # Slugify BOTH sides: "Verify Before Editing" and "verify-before-editing"
        # are the same skill — a case/space variant must not slip the dedup.
        return  # already exists or already proposed — recurrence isn't novelty

    body = (f"# {draft['name']}\n\n{draft['description']}\n\n"
            f"## when to use\n{draft['when_to_use']}\n\n"
            f"## steps\n{draft['steps']}")
    ok, why = await _critique(
        runner, "skill", body,
        f"WEAKNESSES:\n{weaknesses_txt}\n\nEPISODES:\n" + "\n\n".join(episode_summaries)[:2500])
    if not ok:
        runner.report.decisions.append(f"GATE critique dropped skill '{name}': {why}")
        return
    reason = f"hot weakness: {hot[0].name}" if hot else "recurring pattern in episodes"
    proposal = Proposal(
        name=name,
        description=draft["description"][:200],
        project_id=project.id,
        project_name=project.name,
        reason=reason,
        pattern=hot[0].name if hot else "",
        evidence=[public_id(sid) for sid in session_ids],
        body=body,
    )
    runner.report.proposals_drafted += 1
    runner.report.decisions.append(f"PROPOSAL +{name}: {proposal.description[:80]}")
    if not runner.dry_run:
        store.save(proposal)
