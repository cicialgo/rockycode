"""Weakness mining (self-evolve phase 1, slice 3) — Self-Harness's first stage.

After the dream digests a pass's sessions, the failures among them are mined
for RECURRING patterns with causal information, stored as `weakness` memories
(<memory>/weaknesses/*.md, archive-not-delete like everything else). They
surface as index lines in the system prompt and via recall_memory — the
groundwork for the proposals inbox, which will turn hot weaknesses into
prompt/skill proposals.

Two layers, mirroring the judge:
  1. failure_note — free, code-side gate: a session contributes only when it
     shows real failure signals (low judge score, tool/engine errors,
     interrupts, failed tests, or the digest's own "## failed" bullets).
     No signals in the whole pass → no mining call at all.
  2. mine_weaknesses — ONE local Ollama call over the pass's failure notes
     plus the known weaknesses, so recurrence REINFORCES (importance up,
     evidence appended) instead of duplicating. Local on purpose: episode
     digests may carry exit-sheet-derived text, which must never reach a
     cloud model — mining stays on the Ollama side of the split.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from rockycode.dream.core import parse_bullets
from rockycode.memory.store import Memory, _slugify

# Below this judge score a session counts as a failure signal even when the
# mechanical counters look clean (e.g. the agent confidently did the wrong thing).
JUDGE_BAR = 0.7

MAX_PATTERNS = 3  # per pass — weaknesses should be rare and load-bearing

MINING_PROMPT = """\
You maintain a coding agent's short list of its own recurring weaknesses.

FAILURE NOTES from recent sessions:
{notes}

KNOWN WEAKNESSES:
{existing}

Identify at most {max_patterns} RECURRING failure patterns with a likely
cause. Only patterns the notes actually support — no speculation, and an
empty list is a fine answer. If a note is another instance of a KNOWN
weakness, reinforce that one instead of writing a new one.

Reply with ONLY a JSON array (possibly empty):
[{{"pattern": "one-line name of the failure pattern",
   "cause": "likely root cause, one or two sentences",
   "advice": "one actionable instruction that would avoid it",
   "reinforces": "name-of-known-weakness or null"}}]
"""


def failure_note(session: dict, sections: dict) -> Optional[str]:
    """Layer 1 — compact failure evidence for one digested session, or None
    when it shows no failure signals (free; decides whether mining runs)."""
    heur = session.get("heuristic") or {}
    out = session.get("outcome") or {}
    signals: list[str] = []
    if out.get("source") == "judge":
        try:
            score = float(out.get("score", 1.0))
        except (TypeError, ValueError):
            score = 1.0
        if score < JUDGE_BAR:
            signals.append(f"judge score {score} — {str(out.get('rationale', ''))[:200]}")
    if heur.get("tool_errors"):
        signals.append(f"{heur['tool_errors']} tool error(s)")
    if heur.get("engine_errors"):
        signals.append(f"{heur['engine_errors']} engine error(s)")
    if heur.get("interrupts"):
        signals.append(f"{heur['interrupts']} user interrupt(s) mid-work")
    tests = heur.get("tests") or {}
    if tests.get("run", 0) > tests.get("passed", 0):
        signals.append(f"tests: only {tests.get('passed', 0)}/{tests['run']} passed")
    failed = parse_bullets(sections.get("failed", ""))
    if failed:
        signals.append("failed approaches: " + "; ".join(failed[:4]))
    if not signals:
        return None
    task = (sections.get("task") or "(unknown task)").strip().splitlines()[0][:150]
    return f"### {task}\n" + "\n".join(f"- {s}" for s in signals)


def _parse_items(answer: str) -> list[dict]:
    """Lenient array extraction; items missing any required field are dropped
    (a pattern without cause/advice is a vibe, not a weakness)."""
    m = re.search(r"\[.*\]", answer, re.DOTALL)
    if m is None:
        return []
    try:
        arr = json.loads(m.group())
    except json.JSONDecodeError:
        return []
    if not isinstance(arr, list):
        return []
    items = []
    for it in arr:
        if not isinstance(it, dict):
            continue
        if all(isinstance(it.get(k), str) and it[k].strip() for k in ("pattern", "cause", "advice")):
            items.append(it)
    return items[:MAX_PATTERNS]


async def mine_weaknesses(runner, notes: list[tuple[str, str]]) -> None:
    """Layer 2 — one local model call over this pass's failure notes.
    Mutates runner.report; respects runner.dry_run (decisions only)."""
    if not notes:
        return
    existing = [m for m in runner.store.load_all() if m.type == "weakness"]
    existing_txt = "\n".join(f"[{m.name}] {m.description}" for m in existing) or "(none)"
    answer = await runner.chat.chat(
        MINING_PROMPT.format(
            notes="\n\n".join(n for _, n in notes),
            existing=existing_txt,
            max_patterns=MAX_PATTERNS,
        ),
        max_tokens=1024,
    )
    sids = [sid for sid, _ in notes]
    known = {m.name for m in existing}
    for it in _parse_items(answer):
        target = (it.get("reinforces") or "").strip("`'\" ")
        name = target if target in known else _slugify(it["pattern"])
        if name in known:
            # Recurrence: same pattern seen again → more important, more evidence.
            mem = runner.store.get(name)
            runner.report.weaknesses_reinforced += 1
            runner.report.decisions.append(f"WEAKNESS ~{name} (reinforced): {it['pattern'][:80]}")
            if not runner.dry_run and mem is not None:
                mem.importance = min(10, mem.importance + 1)
                mem.evidence.extend(s for s in sids if s not in mem.evidence)
                runner.store.save(mem)
        else:
            runner.report.weaknesses_added += 1
            runner.report.decisions.append(f"WEAKNESS +{name}: {it['pattern'][:80]}")
            if not runner.dry_run:
                runner.store.save(Memory(
                    name=name,
                    type="weakness",
                    description=it["pattern"][:150],
                    importance=6,
                    origin="dream",
                    evidence=list(sids),
                    body=(f"{it['pattern']}\n\n**cause:** {it['cause']}\n"
                          f"**advice:** {it['advice']}"),
                ))
