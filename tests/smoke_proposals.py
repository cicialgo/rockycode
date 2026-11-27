"""The proposals inbox — store, drafting gates, QUALITY GATE, installs. No API.

Contracts under test:
  - ProposalStore: pending → approve installs a global SKILL.md (rk_ evidence
    in frontmatter, discoverable by discover_skills) and files the proposal
    under approved/; archive keeps it; name collisions never overwrite.
  - draft_proposals gates: quiet pass → no model call; hot weakness → one
    pending draft with rk_ evidence; "null" answer → nothing; existing-name
    dedup; the MAX_PENDING cap; --dry-run reports without writing.
  - the quality gate (evolve v2): a critique reject drops the draft (decision
    logged, inbox untouched); a failed dry-run drops a routine draft; an
    unavailable sandbox proposes untested with an honest receipt; newlines in
    a drafted routine prompt round-trip through the emitted toml.
  - amendments REPLACE: approving an amends-proposal overwrites the failing
    routine in place (old toml kept as .prev) — never a -dream sibling.
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


CRITIQUE_OK = json.dumps({"verdict": "ok", "reason": "supported by the evidence"})


class FakeChat:
    """Routes by prompt: drafting prompts get `answer`, the critique gate gets
    `critique` (default: pass). Tests flip `critique` to exercise the gate."""

    def __init__(self, answer="null", critique=CRITIQUE_OK):
        self.answer = answer
        self.critique = critique
        self.prompts = []

    def propose_calls(self):
        return [p for p in self.prompts if "draft SKILL playbooks" in p]

    def critique_calls(self):
        return [p for p in self.prompts if "quality gate" in p]

    async def chat(self, prompt, max_tokens=2048):
        self.prompts.append(prompt)
        if "quality gate" in prompt:
            return self.critique
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

    # ── evolve v2: the quality gate ──────────────────────────────────────────

    # critique reject → the draft never reaches the inbox, decision logged
    r = DreamRunner(WD, chat=FakeChat(
        answer=json.dumps({**DRAFT, "name": "rejected-skill"}),
        critique=json.dumps({"verdict": "reject", "reason": "not supported"})))
    await draft_proposals(r, ["### ep"], ["20260709-000001-aaaaaaaa"])
    assert r.report.proposals_drafted == 0
    assert any("GATE critique dropped" in d for d in r.report.decisions), r.report.decisions
    assert not any(x.name == "rejected-skill" for x in store.list(PENDING, project_id=pid))
    print("gate: a critique reject drops the draft, logs the decision  ✓")

    # unparseable critique → reject (precision first, the pattern stays hot)
    r = DreamRunner(WD, chat=FakeChat(
        answer=json.dumps({**DRAFT, "name": "junk-critique-skill"}), critique="hmm idk"))
    await draft_proposals(r, ["### ep"], ["20260709-000001-aaaaaaaa"])
    assert r.report.proposals_drafted == 0
    print("gate: an unparseable critique verdict rejects, never installs on doubt  ✓")

    # newline in a drafted prompt → the emitted toml still round-trips
    import tomllib as _tomllib
    nl_draft = {"name": "newline-routine", "description": "d", "cadence": "daily",
                "prompt": "line one\nline two"}
    r = DreamRunner(WD, chat=FakeChat(answer=json.dumps(nl_draft)))
    await draft_routine_proposals(r, ["### e1", "### e2"], ["20260709-000001-aaaaaaaa"])
    nl = [x for x in store.list(PENDING, project_id=pid) if x.name == "newline-routine"]
    assert len(nl) == 1, "a newline prompt must survive the lint, not vanish"
    parsed_toml = _tomllib.loads(nl[0].body)
    assert parsed_toml["prompt"] == "line one\nline two", parsed_toml
    assert nl[0].tested == "unavailable", "no client on the fake → honest untested receipt"
    store.archive(nl[0])
    print("gate: model newlines round-trip through the emitted toml  ✓")

    # dry-run receipt: ok → proposed with proof; blocked → dropped
    from rockycode import headless as headless_mod
    from rockycode.routines import dry_run_routine

    async def fake_exec_ok(*, write, **kw):
        write({"type": "result", "steps": 3})
        return 0

    async def fake_exec_blocked(*, write, **kw):
        write({"type": "result", "blocked_on": {"tool": "web_fetch"}})
        return 2

    real_exec = headless_mod.run_exec
    try:
        headless_mod.run_exec = fake_exec_ok
        rr = Routine(name="dry-ok", prompt="do", workdir=str(WD))
        receipt = await dry_run_routine(rr, model="fake", client=object())
        assert receipt["tested"] == "ok" and "3 step(s)" in receipt["note"], receipt

        headless_mod.run_exec = fake_exec_blocked
        receipt = await dry_run_routine(rr, model="fake", client=object())
        assert receipt["tested"] == "failed" and "blocked" in receipt["note"], receipt
    finally:
        headless_mod.run_exec = real_exec
    print("gate: dry-run classifies ok / failed (draft defect) honestly  ✓")

    # a failed dry-run drops the whole routine draft before the inbox
    class ExecFakeChat(FakeChat):
        client = object()
        model = "fake"
    try:
        headless_mod.run_exec = fake_exec_blocked
        r = DreamRunner(WD, chat=ExecFakeChat(answer=json.dumps(
            {"name": "doomed-routine", "description": "d", "cadence": "daily", "prompt": "x"})))
        await draft_routine_proposals(r, ["### e1", "### e2"], ["20260709-000001-aaaaaaaa"])
        assert not any(x.name == "doomed-routine" for x in store.list(PENDING, project_id=pid))
        assert any("GATE dry-run dropped" in d for d in r.report.decisions), r.report.decisions
    finally:
        headless_mod.run_exec = real_exec
    print("gate: a draft that fails its own test never asks for attention  ✓")

    # MCP consolidation may write memory/proposals but must never execute a draft.
    from unittest.mock import patch
    r = DreamRunner(WD, chat=ExecFakeChat(answer=json.dumps({
        "name": "mcp-no-execution", "description": "d", "cadence": "daily", "prompt": "x"})),
        test_routines=False)
    with patch("rockycode.routines.dry_run_routine", side_effect=AssertionError("unexpected execution")):
        await draft_routine_proposals(r, ["### e1", "### e2"], ["20260709-000001-aaaaaaaa"])
    draft = next(p for p in store.list(PENDING, project_id=pid) if p.name == "mcp-no-execution")
    assert draft.tested == "unavailable"
    store.archive(draft)
    print("gate: MCP writes proposals without executing their routine smoke  ✓")

    # ── evolve v2: amendments REPLACE the failing routine ────────────────────
    amend_draft = {"name": "flaky-job", "description": "clearer prompt", "cadence": "daily",
                   "prompt": "do the thing, but read the error first"}
    r = DreamRunner(WD, chat=FakeChat(answer=json.dumps(amend_draft)))
    await draft_routine_proposals(r, ["### e1"], ["20260709-000001-aaaaaaaa"])
    ap = [x for x in store.list(PENDING, project_id=pid) if x.name == "flaky-job"]
    assert len(ap) == 1 and ap[0].amends == "flaky-job", (ap, r.report.decisions)
    old_prompt = rstore.load("flaky-job").prompt
    path = store.approve(ap[0])
    assert path.parent.name == "flaky-job", "amendment must NOT install a -dream sibling"
    fixed = rstore.load("flaky-job")
    assert fixed.prompt == amend_draft["prompt"] and fixed.prompt != old_prompt
    assert (path.parent / "routine.toml.prev").exists(), "the old toml must be kept beside it"
    assert rstore.state(fixed).runs, "run history must survive an amendment"
    print("amend: approve replaces the failing routine in place, history kept  ✓")

    # ── evolve v2: the archive verdict decays the pattern ────────────────────
    from rockycode.dream.patterns import decay_pattern
    assert mem.get("blind-edits").importance == 8
    assert decay_pattern(mem, "blind-edits")
    assert mem.get("blind-edits").importance == 6, "archive must knock the pattern back"
    assert not decay_pattern(mem, "no-such-pattern")
    print("decay: an archived proposal makes its pattern re-earn hotness  ✓")

    # ── evolve v2: the skill watch (usage telemetry → retire proposal) ───────
    from rockycode.dream.proposals import RETIRE_AFTER_SESSIONS, watch_skills
    from rockycode.engine.skills import bump_skill_usage, read_skill_usage

    # enough later sessions (episode evidence sorts after the install stamp)
    for i in range(RETIRE_AFTER_SESSIONS):
        mem.save(Memory(name=f"ep-future-{i}", type="episode", description="e",
                        evidence=[f"29990101-00000{i}-aaaaaaaa"]))
    r = DreamRunner(WD, chat=FakeChat())
    watch_skills(r)
    rp = [x for x in store.list(PENDING, project_id=pid) if x.kind == "retire-skill"]
    assert len(rp) == 1 and rp[0].name == "retire-test-dream-skill", (rp, r.report.decisions)
    assert rp[0].pattern == "test-dream-skill" and "never loaded" in rp[0].description
    assert r.report.proposals_drafted == 1
    print("watch: an unused dream skill draws ONE retire proposal  ✓")

    # a loaded skill is never nagged; an already-proposed retire never repeats
    bump_skill_usage("test-dream-skill-dream")
    assert read_skill_usage()["test-dream-skill-dream"]["loads"] == 1
    r2 = DreamRunner(WD, chat=FakeChat())
    watch_skills(r2)
    assert r2.report.proposals_drafted == 0, \
        "used skills and already-proposed retires must draft nothing"
    print("watch: usage earns keep; retire asks exactly once  ✓")

    # approve retire: the directory moves aside (reversible), discovery forgets it
    path = store.approve(rp[0])
    assert path == skills_home() / ".archived" / "test-dream-skill" and path.is_dir()
    assert not (skills_home() / "test-dream-skill").exists()
    names = {s.name for s in discover_skills(WD, home=Path.home())}
    assert "test-dream-skill" not in names, "a retired skill must leave discovery"
    assert (path / "SKILL.md").exists(), "retire must archive, never delete"
    print("watch: approve retires by MOVING aside — reversible, never deleted  ✓")

    print("PROPOSALS SMOKE OK — drafted, gated, tested, watched; only cici installs. amaze!")


asyncio.run(main())
