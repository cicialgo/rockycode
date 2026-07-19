"""The proposals inbox — store, drafting gates, and skill install. No API.

Contracts under test:
  - ProposalStore: pending → approve installs a global SKILL.md (rk_ evidence
    in frontmatter, discoverable by discover_skills) and files the proposal
    under approved/; archive keeps it; name collisions never overwrite.
  - draft_proposals gates: quiet pass → no model call; hot weakness → one
    pending draft with rk_ evidence; "null" answer → nothing; existing-name
    dedup; the MAX_PENDING cap; --dry-run reports without writing.
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path

# FORCED fresh (not setdefault): this test installs skills into the global
# store, which would pollute smoke_skills' exact-list assert under run_all's
# shared ROCKYCODE_HOME.
os.environ["ROCKYCODE_HOME"] = tempfile.mkdtemp(prefix="rockyhome-")
os.chdir(tempfile.mkdtemp(prefix="rockyprop-"))

from rockycode.dream.core import DreamRunner
from rockycode.dream.proposals import (
    APPROVED, ARCHIVED, MAX_PENDING_PER_PROJECT, PENDING,
    Proposal, ProposalStore, draft_proposals, skills_home,
)
from rockycode.engine.skills import discover_skills
from rockycode.memory.store import Memory, MemoryStore
from rockycode.session import get_project

WD = Path.cwd()

DRAFT = {"name": "verify-before-editing", "description": "read then edit",
         "when_to_use": "whenever editing files", "steps": "- read\n- edit\n- verify"}


class FakeChat:
    def __init__(self, answer="null"):
        self.answer = answer
        self.prompts = []

    def propose_calls(self):
        return [p for p in self.prompts if "draft SKILL playbooks" in p]

    async def chat(self, prompt, max_tokens=2048):
        self.prompts.append(prompt)
        return self.answer


def hot_weakness(store):
    store.save(Memory(name="blind-edits", type="weakness", importance=8,
                      description="edits before reading", origin="dream", body="…"))


async def main():
    pid = get_project(WD).id
    store = ProposalStore()

    # --- store roundtrip: save → list (project-filtered) → approve installs ---
    p = Proposal(name="test-dream-skill", description="a drafted skill",
                 project_id=pid, project_name="prop", reason="hot weakness: blind-edits",
                 evidence=["rk_abc123"], body="# test-dream-skill\n\n## steps\n- do it")
    store.save(p)
    assert len(store.list(PENDING, project_id=pid)) == 1
    assert store.list(PENDING, project_id="other-project") == [], "project filter leaks"

    installed = store.approve(p)
    assert installed == skills_home() / "test-dream-skill" / "SKILL.md" and installed.exists()
    text = installed.read_text(encoding="utf-8")
    assert "origin: dream" in text and "rk_abc123" in text, "provenance missing from SKILL.md"
    assert store.list(PENDING, project_id=pid) == []
    assert len(store.list(APPROVED, project_id=pid)) == 1
    names = {s.name for s in discover_skills(WD, home=Path.home())}
    assert "test-dream-skill" in names, "global rocky skills dir not discovered"
    print("proposals: approve installs a discoverable global skill  ✓")

    # --- collision: never overwrite an existing skill ---
    p2 = Proposal(name="test-dream-skill", description="same name again",
                  project_id=pid, body="# again")
    store.save(p2)
    installed2 = store.approve(p2)
    assert installed2.parent.name == "test-dream-skill-dream", installed2
    assert "do it" in installed.read_text(encoding="utf-8"), "original skill was clobbered!"
    print("proposals: name collisions install aside, never overwrite  ✓")

    # --- archive keeps the file ---
    p3 = Proposal(name="meh-idea", description="d", project_id=pid, body="x")
    store.save(p3)
    store.archive(p3)
    assert store.list(PENDING, project_id=pid) == []
    assert len(store.list(ARCHIVED, project_id=pid)) == 1
    print("proposals: archive keeps provenance, empties pending  ✓")

    # --- drafting gates ---
    mem = MemoryStore.for_workdir(WD)

    # quiet pass: no hot weakness, one episode → no model call at all
    r = DreamRunner(WD, chat=FakeChat())
    await draft_proposals(r, ["### one episode"], ["20260709-000001-aaaaaaaa"])
    assert r.chat.propose_calls() == [] and r.report.proposals_drafted == 0
    print("proposals: a quiet pass drafts nothing, calls nothing  ✓")

    # hot weakness → one draft, rk_ evidence, project stamped
    hot_weakness(mem)
    r = DreamRunner(WD, chat=FakeChat(answer=json.dumps(DRAFT)))
    await draft_proposals(r, ["### ep"], ["20260709-000001-aaaaaaaa"])
    assert r.report.proposals_drafted == 1 and len(r.chat.propose_calls()) == 1
    assert "[blind-edits]" in r.chat.propose_calls()[0], "hot weaknesses feed the prompt"
    drafted = store.list(PENDING, project_id=pid)
    assert len(drafted) == 1 and drafted[0].name == "verify-before-editing"
    assert drafted[0].evidence and drafted[0].evidence[0].startswith("rk_"), \
        "evidence must be rk_ public ids"
    assert drafted[0].reason == "hot weakness: blind-edits"
    assert "## steps" in drafted[0].body
    print("proposals: hot weakness → one pending draft with rk_ evidence  ✓")

    # dedup: same name proposed again → skipped (any-status names count)
    r = DreamRunner(WD, chat=FakeChat(answer=json.dumps(DRAFT)))
    await draft_proposals(r, ["### ep"], ["20260709-000001-aaaaaaaa"])
    assert r.report.proposals_drafted == 0, "re-drafting an existing name must be skipped"
    print("proposals: recurrence isn't novelty — no duplicate drafts  ✓")

    # steps as a JSON ARRAY (observed live: qwen3.5:9b) → normalized to bullets
    from rockycode.dream.proposals import _parse_draft
    listy = json.dumps({**DRAFT, "name": "list-steps-skill",
                        "steps": ["read the file", "edit it", "verify"]})
    parsed = _parse_draft(listy)
    assert parsed is not None and parsed["steps"] == "- read the file\n- edit it\n- verify", \
        "a list-of-strings steps must normalize, not void the draft"
    print("proposals: array steps normalize instead of voiding (live 9B shape)  ✓")

    # "null" answer → nothing
    r = DreamRunner(WD, chat=FakeChat(answer="null"))
    await draft_proposals(r, ["### a", "### b"], ["20260709-000001-aaaaaaaa"])
    assert r.report.proposals_drafted == 0
    print("proposals: the model may decline — null drafts nothing  ✓")

    # cap: MAX_PENDING pending → not even a model call
    for i in range(MAX_PENDING_PER_PROJECT):
        store.save(Proposal(name=f"filler-{i}", description="d", project_id=pid, body="x"))
    r = DreamRunner(WD, chat=FakeChat(answer=json.dumps(
        {**DRAFT, "name": "yet-another-skill"})))
    await draft_proposals(r, ["### ep"], ["20260709-000001-aaaaaaaa"])
    assert r.chat.propose_calls() == [], "a full inbox must stop drafting"
    for i in range(MAX_PENDING_PER_PROJECT):  # clean up fillers
        store.archive(store.list(PENDING, project_id=pid)[0])
    print("proposals: a full inbox earns attention before asking for more  ✓")

    # dry-run: decision recorded, nothing written
    r = DreamRunner(WD, chat=FakeChat(answer=json.dumps(
        {**DRAFT, "name": "dry-run-skill"})), dry_run=True)
    await draft_proposals(r, ["### ep"], ["20260709-000001-aaaaaaaa"])
    assert r.report.proposals_drafted == 1 and any("PROPOSAL" in d for d in r.report.decisions)
    assert not any(x.name == "dry-run-skill" for x in store.list(PENDING, project_id=pid))
    print("proposals: --dry-run previews without writing  ✓")

    # ── slice 4: routine proposals (draft → install a routine.toml) ──────────
    from rockycode.dream.proposals import draft_routine_proposals, failing_routines
    from rockycode.routines import Routine, RoutineStore, routines_dir

    ROUTINE_DRAFT = {"name": "arxiv-digest", "description": "digest new papers each morning",
                     "cadence": "daily", "prompt": "fetch my arxiv watchlist and summarize"}
    r = DreamRunner(WD, chat=FakeChat(answer=json.dumps(ROUTINE_DRAFT)))
    await draft_routine_proposals(r, ["### ep one", "### ep two"], ["20260709-000001-aaaaaaaa"])
    rp = [x for x in store.list(PENDING, project_id=pid) if x.kind == "routine"]
    assert len(rp) == 1 and rp[0].name == "arxiv-digest", rp
    assert "prompt = " in rp[0].body and "auto = false" in rp[0].body, "body is a routine.toml, no auto"
    print("routine proposal: dream drafts kind=routine with a routine.toml body  ✓")

    # approve installs a routine.toml (NOT auto, NO lease — click-to-run only)
    path = store.approve(rp[0])
    assert path.name == "routine.toml" and path.parent.parent == routines_dir()
    installed = RoutineStore().load("arxiv-digest")
    assert installed is not None and installed.auto is False and installed.lease_deadline == 0.0, \
        "a dream-drafted routine must never self-run — no auto, no lease"
    assert installed.prompt == ROUTINE_DRAFT["prompt"]
    print("routine proposal: approve installs routine.toml, click-to-run only  ✓")

    # ── slice 4: the failing-routine watch → amendment signal ────────────────
    rstore = RoutineStore()
    bad = Routine(name="flaky-job", description="keeps breaking", prompt="do the thing",
                  project_id=pid, workdir=str(WD), enabled=True)
    rstore.save(bad)
    for i, stt in enumerate(["error", "done", "blocked", "budget"]):
        rstore.record_run(bad, session_id=f"r{i}", cost=0.01, status=stt)
    fails = failing_routines(pid)
    assert any(f["routine"].name == "flaky-job" and f["fails"] >= 2 for f in fails), fails
    print("routine watch: a routine failing across recent runs is flagged for amendment  ✓")

    print("PROPOSALS SMOKE OK — dream drafts skills AND routines, only cici installs. amaze!")


asyncio.run(main())
