"""The pattern accumulator (evolve v2) — pure code, no model call.

Reflect (dream/reflect.py) already does the judgment inside the session's own
warm call: it names patterns and matches them against the known index
(reinforces: name-or-null). What is left for dream time is bookkeeping, and
bookkeeping must be deterministic:

  - recurrence REINFORCES (importance +1, capped, evidence appended) instead
    of duplicating — same rule the old miner had;
  - recurrence is counted per SESSION, never per checkpoint: a session's
    reflect records are merged by pattern name first, and a session id
    already in a memory's evidence never bumps importance again. Three
    checkpoints seeing the same habit is one observation, not a hot streak
    (the anti-sediment rule, locked 2026-08-02);
  - an ARCHIVED pattern decays instead of resurrecting: when the user
    archives a proposal, the pattern that drafted it gets its importance
    knocked back (see decay_pattern) — "no" is a signal too.

Two kinds share the machinery: "weakness" = the agent keeps failing the same
way (skill material), "habit" = the user keeps doing some task by hand
(skill or routine material). Both live in the memory store, archive-not-
delete like everything else.
"""
from __future__ import annotations

from typing import Optional

from rockycode.memory.store import Memory, _slugify

# Below this reflect score a session counts as a failure signal even when the
# mechanical counters look clean (the agent confidently did the wrong thing).
SCORE_BAR = 0.7

NEW_IMPORTANCE = 6      # first sighting — one reinforcement away from hot
IMPORTANCE_CAP = 10
DECAY = 2               # an archived proposal knocks its pattern back this much


def merge_session_patterns(reflects: list[dict]) -> list[dict]:
    """Merge one session's reflect records into per-name pattern items —
    the per-session dedupe. Later checkpoints win on wording (they saw more);
    a reinforces target seen in ANY checkpoint sticks."""
    merged: dict[str, dict] = {}
    for rec in reflects:
        for it in rec.get("patterns") or []:
            name = _slugify(str(it.get("reinforces") or it.get("name") or ""))
            if not name:
                continue
            prev = merged.get(name)
            item = {**(prev or {}), **{k: v for k, v in it.items() if v}}
            merged[name] = item
    return list(merged.values())


def apply_patterns(runner, session_id: str, patterns: list[dict]) -> None:
    """Fold one session's merged patterns into the store. Mutates
    runner.report; respects runner.dry_run (decisions only)."""
    existing = {m.name: m for m in runner.store.load_all()
                if m.type in ("weakness", "habit")}
    for it in patterns:
        target = _slugify(str(it.get("reinforces") or ""))
        name = target if target in existing else _slugify(str(it.get("name", "")))
        if not name:
            continue
        kind = it.get("kind") if it.get("kind") in ("weakness", "habit") else "weakness"
        mem = existing.get(name)
        if mem is not None:
            if session_id in mem.evidence:
                continue  # this session already counted — checkpoints don't stack
            runner.report.patterns_reinforced += 1
            runner.report.decisions.append(f"PATTERN ~{name} (reinforced): {it.get('description', '')[:80]}")
            if not runner.dry_run:
                mem.importance = min(IMPORTANCE_CAP, mem.importance + 1)
                mem.evidence.append(session_id)
                runner.store.save(mem)
        else:
            runner.report.patterns_added += 1
            runner.report.decisions.append(f"PATTERN +{name} ({kind}): {it.get('description', '')[:80]}")
            advice = str(it.get("advice", "")).strip()
            body = str(it.get("description", "")).strip()
            if advice:
                body += f"\n\n**advice:** {advice}"
            if not runner.dry_run:
                mem = Memory(
                    name=name, type=kind,
                    description=str(it.get("description", ""))[:150],
                    importance=NEW_IMPORTANCE, origin="dream",
                    evidence=[session_id], body=body,
                )
                runner.store.save(mem)
                existing[name] = mem


def decay_pattern(store, name: str) -> bool:
    """The archive-verdict feedback: the user said no to a proposal, so the
    pattern behind it must re-earn hotness (importance knocked back, evidence
    kept). Returns True when a pattern was found and decayed."""
    mem = store.get(name)
    if mem is None or mem.type not in ("weakness", "habit"):
        return False
    mem.importance = max(1, mem.importance - DECAY)
    store.save(mem)
    return True


def failure_note(session: dict, data: dict) -> Optional[str]:
    """Grounded failure evidence for one session, as the lines of its episode
    note's `## failure signals` section, or None when it shows none.

    The reflect grade is a model's opinion; these are what happened: the
    grader's reason for a low score, and the harness counters from the
    session's heuristic record (tool and engine errors, interrupts mid-work,
    test runs that did not pass). Free — reads what is already recorded,
    calls nothing. The approaches that failed are the note's own `## failed`
    section and are not repeated here."""
    heur = session.get("heuristic") or {}
    signals: list[str] = []
    score = data.get("score")
    if isinstance(score, (int, float)) and score < SCORE_BAR:
        why = " ".join(str(data.get("rationale") or "").split())[:200]
        signals.append(f"graded {score}" + (f": {why}" if why else ""))
    if heur.get("tool_errors"):
        signals.append(f"{heur['tool_errors']} tool error(s)")
    if heur.get("engine_errors"):
        signals.append(f"{heur['engine_errors']} engine error(s)")
    if heur.get("interrupts"):
        signals.append(f"{heur['interrupts']} user interrupt(s) mid-work")
    tests = heur.get("tests") or {}
    if tests.get("run", 0) > tests.get("passed", 0):
        signals.append(f"test runs: {tests.get('passed', 0)} of {tests['run']} passed")
    return "\n".join(f"- {s}" for s in signals) if signals else None
