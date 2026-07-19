"""The layered multi-angle judge (self-evolve phase 1, slice 2).

Grades a finished session's transcript into an `outcome` record
(source="judge") appended to that session's trajectory — the dream-grade
reward that sits after phase 0's heuristic one. Readers take the LAST
outcome record, so judge > heuristic without rewriting anything.

Layered, so the cloud is the last resort, not the first:
  1. gate   — free: the heuristic counters decide whether there is anything
              to grade. Single-turn tool-less sessions, sessions that predate
              outcome capture, and already-judged sessions are skipped,
              not billed.
  2. angles — ONE call on an OpenAI-compatible client (the session's own —
              chat and `rockycode dream` reuse existing auth, no new keys):
              completion, adherence, efficiency, user_feeling. The last is
              inferred from BEHAVIOR in the transcript — corrections,
              repeated asks, interrupts, thanks, abandonment.
  3. score  — code, not model: a fixed-weight aggregate, auditable and
              stable across judge-model upgrades.

PRIVACY: the transcript comes from condense() with its default
feedback=False — the exit sheet is never in a judge prompt (the DeepSeek-
for-transcript / Ollama-for-sheet split, see the trajectory feedback
contract). Only the local Ollama dream reads the sheet.
"""
from __future__ import annotations

import json
import re
import time
from typing import Optional

from rockycode.dream.core import condense

ANGLES = ("completion", "adherence", "efficiency", "user_feeling")

# The aggregate lives in code so a score of 0.78 means the same thing next
# month. Completion dominates (the harness exists to finish tasks); feeling
# outweighs efficiency because a happy slow session beats a fast wrong one.
WEIGHTS = {"completion": 0.40, "adherence": 0.25, "efficiency": 0.15, "user_feeling": 0.20}

JUDGE_PROMPT = """\
You are grading one finished coding-agent session from its condensed
transcript. No user rating is available to you — infer the user's
experience from behavior alone (corrections, repeated asks, interrupts,
gratitude, abandonment). A trailing [outcome] line holds mechanical
counters (tool errors, denials, interrupts, tests) — use it as evidence.

TRANSCRIPT:
{transcript}

Score each angle from 0.0 (bad) to 1.0 (great):
- completion: did the session accomplish what the user asked for?
- adherence: did the agent follow instructions and stay in scope?
- efficiency: was the path direct — few wasted steps, errors, retries?
- user_feeling: how satisfied does the user's BEHAVIOR suggest they were?

Reply with ONLY a JSON object, no prose around it:
{{"completion": 0.0, "adherence": 0.0, "efficiency": 0.0, "user_feeling": 0.0,
  "rationale": "one or two sentences citing transcript evidence"}}
"""


def gate(session: dict) -> bool:
    """Layer 1 — anything to grade? Free, and fails closed: no heuristic
    outcome record (pre-phase-0 session, or one that never really ran) means
    no cloud call. An existing judge record means the same (a --dry-run pass
    or a crash between append and digest must not double-bill)."""
    out = session.get("outcome") or {}
    if out.get("source") != "heuristic":
        return False
    return out.get("tool_calls", 0) >= 1 or out.get("turns", 0) >= 2


def _parse(answer: str) -> Optional[dict]:
    """Lenient JSON extraction: the first {...} block, clamped angles.
    Any missing/non-numeric angle voids the whole grade — a partial score
    would silently skew the aggregate."""
    m = re.search(r"\{.*\}", answer, re.DOTALL)
    if m is None:
        return None
    try:
        obj = json.loads(m.group())
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    angles: dict[str, float] = {}
    for a in ANGLES:
        try:
            angles[a] = max(0.0, min(1.0, float(obj[a])))
        except (KeyError, TypeError, ValueError):
            return None
    return {"angles": angles, "rationale": str(obj.get("rationale", "")).strip()[:400]}


class TranscriptJudge:
    """Wraps an OpenAI-compatible client the caller already owns — the live
    engine's in the TUI, a require_key() one in `rockycode dream`. No key
    available → the caller simply passes no judge and dream stays local."""

    def __init__(self, client, model: str) -> None:
        self.client = client
        self.model = model

    async def grade(self, session: dict) -> Optional[dict]:
        """One judge outcome dict, or None (gated out / call failed / bad
        JSON). Never raises — a failed grade must not stop the dream pass."""
        if not gate(session):
            return None
        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                # condense() default: feedback stays OUT of this cloud prompt.
                messages=[{"role": "user", "content": JUDGE_PROMPT.format(
                    transcript=condense(session))}],
                max_tokens=512,
                extra_body={"thinking": {"type": "disabled"}},  # cheap + fast, like loop.py's off-mode
            )
            answer = (resp.choices[0].message.content or "") if resp.choices else ""
        except Exception:  # noqa: BLE001 — API trouble = no grade, dream goes on
            return None
        parsed = _parse(answer)
        if parsed is None:
            return None
        score = round(sum(WEIGHTS[a] * parsed["angles"][a] for a in ANGLES), 4)
        return {
            "source": "judge",
            "graded_at": time.time(),
            "judge_model": self.model,
            "angles": parsed["angles"],
            "score": score,
            "rationale": parsed["rationale"],
        }
