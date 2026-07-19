"""The dream-time transcript judge (self-evolve phase 1, slice 2) — no API.

Contracts under test:
  - gate (layer 1) is free and fails closed: trivial / pre-phase-0 /
    already-judged sessions never reach the cloud.
  - grade (layer 2) sends ONE prompt that carries the heuristic outcome but
    NEVER the exit sheet (the DeepSeek-for-transcript / Ollama-for-sheet split).
  - score (layer 3) is fixed-weight code, and lenient parsing voids partial
    or malformed grades instead of skewing the aggregate.
  - DreamRunner appends the judge outcome to the trajectory file before
    digesting (the episode sees the verdict); --dry-run stays free.
"""
import asyncio
import json
import os
import tempfile
import types
from pathlib import Path

os.environ.setdefault("ROCKYCODE_HOME", tempfile.mkdtemp(prefix="rockyhome-"))
os.chdir(tempfile.mkdtemp(prefix="rockyjudge-"))

from rockycode import session as _session
from rockycode.dream.core import DreamRunner
from rockycode.dream.judge import ANGLES, WEIGHTS, TranscriptJudge, _parse, gate
from rockycode.session import get_project

WD = Path.cwd()

GOOD_ANSWER = (
    'Sure! Here is the grade:\n{"completion": 0.9, "adherence": 0.8, '
    '"efficiency": 0.6, "user_feeling": 0.7, "rationale": "task done, one retry"}'
)


class FakeCompletions:
    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    async def create(self, **kw):
        self.prompts.append(kw["messages"][0]["content"])
        msg = types.SimpleNamespace(content=self.answer)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


def make_client(answer=GOOD_ANSWER):
    fc = FakeCompletions(answer)
    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=fc)), fc


def sess(outcome, feedback=None):
    return {
        "session_id": "s1", "path": "unused", "meta": {},
        "messages": [
            {"role": "user", "content": "fix the flaky login test"},
            {"role": "assistant", "content": "fixed it"},
        ],
        "outcome": outcome, "feedback": feedback,
    }


HEUR = {"source": "heuristic", "turns": 2, "tool_calls": 3, "tool_errors": 1}


class FakeChat:
    """The Ollama side (digest/state) — captures prompts for the privacy canary."""

    def __init__(self):
        self.prompts = []

    async def chat(self, prompt, max_tokens=2048):
        self.prompts.append(prompt)
        if "consolidating a coding agent" in prompt:
            return ("## task\nfix login test\n## outcome\nsuccess\n## worked\n- none\n"
                    "## failed\n- none\n## facts\n- none\n## importance\n7")
        return "- project state line"


async def main():
    # --- layer 1: the gate is free and fails closed ---
    assert not gate(sess(None)), "no outcome record → no cloud call"
    assert not gate(sess({"source": "heuristic", "turns": 1, "tool_calls": 0})), "trivial → skip"
    assert not gate(sess({"source": "judge", "score": 0.5})), "already judged → skip"
    assert gate(sess(HEUR))
    client, fc = make_client()
    judge = TranscriptJudge(client, "fake-judge")
    assert await judge.grade(sess(None)) is None and fc.prompts == [], "gated grade must not bill"
    print("judge: the gate fails closed, nothing trivial is billed  ✓")

    # --- layer 2+3: one call, evidence in, sheet NEVER in, code-side score ---
    graded = await judge.grade(sess(HEUR, feedback={"mood": "good", "text": "loved it"}))
    assert graded is not None and len(fc.prompts) == 1
    prompt = fc.prompts[0]
    assert "[outcome]" in prompt, "heuristic counters are judge evidence"
    assert "loved it" not in prompt and "exit-feedback" not in prompt, \
        "the exit sheet leaked into a cloud prompt!"
    assert graded["source"] == "judge" and graded["judge_model"] == "fake-judge"
    expected = round(sum(WEIGHTS[a] * v for a, v in
                         zip(ANGLES, (0.9, 0.8, 0.6, 0.7))), 4)
    assert graded["score"] == expected == 0.79, graded["score"]
    assert graded["rationale"] == "task done, one retry" and graded["graded_at"] > 0
    print("judge: one call, outcome in, sheet out, fixed-weight score  ✓")

    # --- lenient parse: partial or malformed grades are voided, values clamped ---
    assert _parse("no json here at all") is None
    assert _parse('{"completion": 0.9}') is None, "missing angles must void the grade"
    clamped = _parse('{"completion": 1.7, "adherence": -2, "efficiency": 0.5, '
                     '"user_feeling": 0.5, "rationale": "r"}')
    assert clamped["angles"]["completion"] == 1.0 and clamped["angles"]["adherence"] == 0.0
    bad_client, bad_fc = make_client(answer="the model rambled, no json")
    assert await TranscriptJudge(bad_client, "m").grade(sess(HEUR)) is None
    print("judge: malformed grades void cleanly, angles clamp  ✓")

    # --- runner integration: judge → append → digest sees the verdict ---
    pid = get_project(WD).id
    traj = _session.global_traj_dir()
    traj.mkdir(parents=True, exist_ok=True)
    lines = [
        {"t": 1, "kind": "meta", "data": {"source": "chat", "project_id": pid,
                                          "workdir": str(WD), "model": "fake"}},
        {"t": 2, "kind": "message", "data": {"role": "system", "content": "sys"}},
        {"t": 3, "kind": "message", "data": {"role": "user", "content": "fix the login test"}},
        {"t": 4, "kind": "message", "data": {"role": "assistant", "content": "fixed"}},
        {"t": 5, "kind": "outcome", "data": HEUR},
        {"t": 6, "kind": "feedback", "data": {"mood": "good", "text": "loved it",
                                              "local_only": True}},
    ]
    p = traj / "20260708-000003-cccccccc.jsonl"
    p.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")

    # dry-run first: a preview must be FREE — no judge call, no file change
    client, fc = make_client()
    dry = DreamRunner(WD, chat=FakeChat(), dry_run=True,
                      judge=TranscriptJudge(client, "fake-judge"))
    report = await dry.run(limit=5)
    assert report.sessions_judged == 0 and fc.prompts == [], "dry-run billed the judge!"
    assert '"judge"' not in p.read_text(encoding="utf-8")
    print("judge: --dry-run stays free, nothing appended  ✓")

    # real pass: judged, appended, and the LOCAL digest sees sheet + verdict
    client, fc = make_client()
    ollama = FakeChat()
    runner = DreamRunner(WD, chat=ollama, judge=TranscriptJudge(client, "fake-judge"))
    report = await runner.run(limit=5)
    assert report.sessions_judged == 1 and report.sessions_digested == 1, report
    recs = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
    outcomes = [r["data"] for r in recs if r["kind"] == "outcome"]
    assert len(outcomes) == 2 and outcomes[-1]["source"] == "judge", \
        "judge outcome must be appended after the heuristic one (last wins)"
    assert outcomes[-1]["score"] == 0.79
    digest_prompt = ollama.prompts[0]
    assert "loved it" in digest_prompt, "the LOCAL digest is allowed to see the sheet"
    assert '"score": 0.79' in digest_prompt, "the digest should see the judge's verdict"
    assert any("JUDGE" in d for d in report.decisions), report.decisions
    print("judge: runner appends the verdict, local digest reads sheet + score  ✓")

    print("JUDGE SMOKE OK — graded in the cloud, dreamed at home. amaze!")


asyncio.run(main())
