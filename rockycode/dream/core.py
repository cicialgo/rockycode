"""The dream pass (evolve v2): offline consolidation over reflect records.

Reflect (dream/reflect.py) captures each session warm, on the session's own
model — grade, episode, facts, pattern observations, all as `reflect`
trajectory records. The dream is the OFFLINE half only: at launch (catch-up,
config `evolve = auto`) or via `rockycode dream`, it

  1. cold-reflects stragglers — sessions that ended without a reflect record
     (hard kill, pre-v2 trajectory): one call over the condensed transcript
  2. consolidates — each un-dreamed session becomes an episode note; facts
     are reconciled against existing memories (NOOP/ADD/UPDATE/ARCHIVE,
     archive never deletes); patterns fold into the store (dream/patterns.py,
     pure code — recurrence reinforces, counted once per session)
  3. drafts proposals — hot patterns become skill/routine drafts in the
     pending inbox (dream/proposals.py; the human installs, never the dream)
  4. maintains the project digest — the ONE dream-owned section of MEMORY.md
     — and re-embeds the index when something changed

Every model call here runs on the same client the session used (CloudChat
adapter, thinking off or the model's lowest supported tier) — no second stack or key. v2
removed the judge/digest/mining trio (reflect does their jobs warmer and
cheaper) and the exit sheet (inbox verdicts are the human signal now).

"Already dreamed" is derived from the files, not a state db: a session id
listed in any episode's `evidence:` has been consolidated. Delete the episode
file and the session becomes dreamable again — files stay the truth.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from rockycode.memory.store import Memory, MemoryStore, _slugify

TRAJECTORY_DIRNAME = Path(".rockycode") / "trajectories"
LOCK_STALE_S = 3600
MIN_SESSION_MESSAGES = 3          # system+user only → nothing to learn
MAX_TRANSCRIPT_CHARS = 6_000
MAX_STATE_LINES = 25

DREAM_MARK_START = "<!-- dream:state -->"
DREAM_MARK_END = "<!-- /dream:state -->"

RECONCILE_PROMPT = """\
A coding agent wants to save a new memory. Decide how it relates to the
existing memories below.

NEW FACT: {fact}

EXISTING MEMORIES:
{existing}

Reply with EXACTLY one decision on the first line:
NOOP            — already covered by an existing memory
ADD             — genuinely new information, save it alongside
UPDATE <name>   — improves/extends that memory. Then a line with only ---
                  followed by the full merged memory text.
ARCHIVE <name>  — that memory is now wrong or obsolete; the new fact replaces it
"""

STATE_PROMPT = """\
You maintain the "current state" section of a coding project's memory file.

CURRENT STATE SECTION (may be empty):
{current}

NEW EPISODE NOTES SINCE LAST UPDATE:
{episodes}

Rewrite the state section: what the project is, current focus, recent work,
known gotchas. Markdown bullets only, at most {max_lines} lines, dense,
no preamble, no heading.
"""


class CloudChat:
    """Prompt-in/text-out adapter over the session's own OpenAI-compatible
    client — every dream job speaks this shape (tests fake it with a .chat).
    Thinking off where supported, otherwise the model's lowest tier."""

    def __init__(self, client, model: str, *, model_choice=None) -> None:
        self.client = client
        self.model = model
        self.model_choice = model_choice
        self.last_error = ""

    async def chat(self, prompt: str, max_tokens: int = 2048) -> str:
        if self.model_choice is not None:
            if self.last_error:
                raise RuntimeError(self.last_error)
            from rockycode.model_selection import text_completion
            try:
                return await text_completion(self.client, self.model_choice,
                                             [{"role": "user", "content": prompt}], max_tokens)
            except Exception as exc:
                self.last_error = str(exc)
                raise
        from rockycode.engine.auxiliary import request_options, response_text
        resp = await self.client.chat.completions.create(
            **request_options(self.model, max_tokens),
            messages=[{"role": "user", "content": prompt}],
        )
        return response_text(resp)


@dataclass
class DreamReport:
    sessions_digested: int = 0
    sessions_cold: int = 0            # stragglers that billed a cold reflect
    sessions_skipped: int = 0
    facts_added: int = 0
    facts_updated: int = 0
    facts_archived: int = 0
    facts_noop: int = 0
    patterns_added: int = 0
    patterns_reinforced: int = 0
    proposals_drafted: int = 0
    state_updated: bool = False
    reindexed: Optional[tuple[int, int, int]] = None
    decisions: list[str] = field(default_factory=list)


# ---- trajectory reading --------------------------------------------------------

def load_session(path: Path) -> Optional[dict]:
    meta, messages, outcome, heuristic = {}, [], None, None
    reflects: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("kind") == "meta":
                meta = rec.get("data", {})
            elif rec.get("kind") == "message":
                messages.append(rec.get("data", {}))
            elif rec.get("kind") == "outcome":
                outcome = rec.get("data", {})  # last wins: reflect > heuristic
                if outcome.get("source") == "heuristic":
                    heuristic = outcome  # kept separately: patterns.failure_note reads its counters
            elif rec.get("kind") == "reflect":
                reflects.append(rec.get("data", {}))
    except OSError:
        return None
    if len(messages) < MIN_SESSION_MESSAGES:
        return None
    if not any(m.get("role") == "assistant" for m in messages):
        return None
    return {
        "session_id": path.stem, "path": str(path), "meta": meta,
        "messages": messages, "outcome": outcome, "heuristic": heuristic,
        "reflects": reflects,
    }


def condense(session: dict) -> str:
    parts: list[str] = []
    for msg in session["messages"]:
        role, content = msg.get("role"), msg.get("content") or ""
        if role == "user":
            parts.append(f"[user] {content[:1200]}")
        elif role == "assistant":
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                args = (fn.get("arguments") or "").replace("\n", " ")[:120]
                parts.append(f"[{fn.get('name', 'tool')}] {args}")
            if content:
                parts.append(f"[rocky] {content[:400]}")
        elif role == "tool":
            first = content.strip().splitlines()[0][:160] if content.strip() else ""
            if first.startswith(("[error]", "[timeout]", "[exit")) and not first.startswith("[exit 0]"):
                parts.append(f"  ↳ {first}")
    if session["outcome"]:
        parts.append(f"[outcome] {json.dumps(session['outcome'], ensure_ascii=False)[:400]}")
    text = "\n".join(parts)
    if len(text) > MAX_TRANSCRIPT_CHARS:
        half = MAX_TRANSCRIPT_CHARS // 2
        text = f"{text[:half]}\n… [middle of session omitted] …\n{text[-half:]}"
    return text


def merged_reflect(session: dict) -> Optional[dict]:
    """One consolidated view of a session's reflect records, or None when it
    has none (a straggler). Episode/score/summary come from the LAST
    checkpoint (it saw the most); facts union across checkpoints (they are
    deltas by contract — see reflect.DELTA_NOTE)."""
    recs = session.get("reflects") or []
    if not recs:
        return None
    last = recs[-1]
    facts: list[str] = []
    skills: list[str] = []
    for rec in recs:
        facts.extend(f for f in rec.get("facts") or [] if f not in facts)
        skills.extend(s for s in rec.get("skills_used") or [] if s not in skills)
    return {**last, "facts": facts, "skills_used": skills}


def _same_provider(recorded_model, current_model) -> bool:
    """True unless both models resolve to DIFFERENT providers. Unknown or
    unresolvable ids compare as strings; a legacy session without a model
    passes (nothing to compare against)."""
    if not recorded_model or not current_model:
        return True
    if recorded_model == current_model:
        return True
    try:
        from rockycode.engine.providers import resolve
        a, b = resolve(str(recorded_model)), resolve(str(current_model))
    except Exception:  # noqa: BLE001 — a registry hiccup must not leak data
        return False
    if a is None or b is None:
        return False
    return getattr(a[0], "name", a[0]) == getattr(b[0], "name", b[0])


def keyword_neighbors(store: MemoryStore, fact: str, k: int = 3) -> list[Memory]:
    """No-embeddings neighbor lookup: rank by content-word overlap. A whole-
    string substring match (store.search) never hits paraphrased facts."""
    words = {w for w in re.findall(r"\w+", fact.lower()) if len(w) > 2}
    words |= set(re.findall(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]", fact))
    scored: list[tuple[int, Memory]] = []
    for mem in store.load_all():
        if mem.type == "episode":
            continue
        text = f"{mem.name} {mem.description} {mem.body}".lower()
        hits = sum(1 for w in words if w in text)
        if hits >= 2:
            scored.append((hits, mem))
    scored.sort(key=lambda t: -t[0])
    return [m for _, m in scored[:k]]


# ---- the runner ----------------------------------------------------------------

class DreamRunner:
    def __init__(
        self,
        workdir: Path,
        *,
        client=None,
        model: str = "",
        chat=None,
        dry_run: bool = False,
        test_routines: bool = True,
        log: Callable[[str], None] = lambda s: None,
        exclude: Optional[set[str]] = None,
        share_project_context: bool = False,
    ) -> None:
        self.workdir = workdir
        self.store = MemoryStore.for_workdir(workdir)
        # The session's own client, adapter-wrapped; tests inject `chat`.
        self.chat = chat or CloudChat(client, model)
        self.dry_run = dry_run
        self.test_routines = test_routines
        # Host-approved transfer of project transcripts AND derived memory/skills.
        self.share_project_context = share_project_context
        self.log = log
        # Session ids to leave alone this pass — the TUI's launch catch-up
        # excludes the LIVE session (its final reflect doesn't exist yet).
        self.exclude = exclude or set()
        self.report = DreamReport()

    # -- lockfile (single writer across TUI-idle and manual runs) --------------

    @property
    def _lock(self) -> Path:
        return self.store.root / ".dream.lock"

    def _acquire_lock(self) -> bool:
        if self._lock.exists() and time.time() - self._lock.stat().st_mtime < LOCK_STALE_S:
            return False
        self._lock.parent.mkdir(parents=True, exist_ok=True)
        self._lock.write_text(str(os.getpid()), encoding="utf-8")
        return True

    def _release_lock(self) -> None:
        try:
            self._lock.unlink(missing_ok=True)
        except OSError:
            pass

    # -- helpers ----------------------------------------------------------------

    def _digested_ids(self) -> set[str]:
        ids: set[str] = set()
        for mem in self.store.load_all(include_archived=True):
            if mem.type == "episode":
                ids.update(mem.evidence)
        return ids

    def _pending_sessions(self, limit: int) -> list[dict]:
        from rockycode import session as _session
        traj_dir = _session.global_traj_dir()
        if not traj_dir.is_dir():
            return []
        pid = _session.get_project(self.workdir).id
        digested = self._digested_ids()
        sessions = []
        for path in sorted(traj_dir.glob("*.jsonl")):
            if path.stem in digested or path.stem in self.exclude:
                continue
            session = load_session(path)
            if session is None:
                self.report.sessions_skipped += 1
                continue
            # global store → only consolidate THIS project's sessions (no
            # cross-project memory contamination).
            if session["meta"].get("project_id") != pid:
                continue
            sessions.append(session)
            if len(sessions) >= limit:
                break
        return sessions

    def _known_patterns(self) -> list[tuple[str, str]]:
        return [(m.name, m.description) for m in self.store.load_all()
                if m.type in ("weakness", "habit")]

    # -- job 1: consolidate one session -------------------------------------------

    async def consolidate(self, session: dict) -> Optional[tuple[list[str], str]]:
        """Fold one session into the store: cold-reflect if it's a straggler,
        save the episode note, apply its patterns. Returns (facts, episode
        summary for the state prompt), or None when the session yields
        nothing (cold reflect failed)."""
        from rockycode.dream.patterns import SCORE_BAR, apply_patterns, failure_note, merge_session_patterns
        from rockycode.dream.reflect import run_reflect_cold
        from rockycode.engine.trajectory import append_record

        sid = session["session_id"]
        data = merged_reflect(session)
        if data is None:
            # Privacy guard: a cold reflect re-sends a stored transcript. Only the
            # provider that already saw the session gets to see it again — with
            # rocky now multi-model, a later launch on another vendor must not
            # quietly ship old conversations there. Such stragglers stay pending.
            if not self.share_project_context and not _same_provider(
                    session["meta"].get("model"), getattr(self.chat, "model", "")):
                self.report.sessions_skipped += 1
                self.report.decisions.append(f"SKIP cold-reflect {sid[:15]}: recorded with another provider")
                return None
            data = await run_reflect_cold(self.chat, condense(session),
                                          known_patterns=self._known_patterns())
            if data is None:
                self.report.sessions_skipped += 1
                return None
            self.report.sessions_cold += 1
            self.report.decisions.append(f"COLD-REFLECT {sid[:15]}: score {data['score']}")
            if not self.dry_run:
                # The records make the session warm forever: re-runs are free,
                # and the graded outcome supersedes the heuristic (last wins).
                append_record(Path(session["path"]), "reflect", data)
                append_record(Path(session["path"]), "outcome", {
                    "source": "reflect", "seq": data["seq"], "score": data["score"],
                    "angles": data["angles"], "rationale": data["rationale"],
                })
            session = {**session, "reflects": [data]}

        episode = data.get("episode") or {}
        task = episode.get("task") or "(unknown task)"
        score = data.get("score", 0.0)
        sections = [
            f"## task\n{task}",
            f"## outcome\n{episode.get('outcome', 'partial')} — reflect score {score}",
            "## worked\n" + ("\n".join(f"- {w}" for w in episode.get("worked") or []) or "- none"),
            "## failed\n" + ("\n".join(f"- {w}" for w in episode.get("failed") or []) or "- none"),
        ]
        # What the harness saw, beside what the grader thought (a clean session adds nothing).
        signals = failure_note(session, data)
        if signals:
            sections.append(f"## failure signals\n{signals}")
        body = "\n\n".join(sections)
        note = Memory(
            name=f"ep-{sid[:15]}",
            type="episode",
            description=task.splitlines()[0][:150],
            # A failed session is worth more to remember than a routine success.
            importance=6 if score < SCORE_BAR else 5,
            origin="dream",
            evidence=[sid],
            body=body,
        )
        self.report.decisions.append(f"episode ep-{sid[:15]}: {note.description}")
        if not self.dry_run:
            self.store.save(note)
        self.report.sessions_digested += 1

        apply_patterns(self, sid, merge_session_patterns(session["reflects"]))
        return list(data.get("facts") or []), f"### {note.description}\n{body[:600]}"

    # -- job 3: reconciliation ----------------------------------------------------

    async def reconcile(self, fact: str, sid: str, index=None) -> None:
        neighbors: list[Memory] = []
        if index is not None:
            try:
                neighbors = [m for m, _ in await index.search(fact, k=3)]
            except Exception:  # noqa: BLE001 — fall back to substring neighbors
                neighbors = []
        if not neighbors:
            neighbors = keyword_neighbors(self.store, fact)
        neighbors = [m for m in neighbors if m.type != "episode"]

        def add() -> None:
            self.report.facts_added += 1
            self.report.decisions.append(f"ADD: {fact[:90]}")
            if not self.dry_run:
                self.store.save(Memory(
                    name=_slugify(fact), type="fact", description=fact[:150],
                    origin="dream", evidence=[sid], body=fact,
                ))

        if not neighbors:
            add()
            return

        existing = "\n".join(f"[{m.name}] ({m.type}) {m.description}: {m.body[:300]}" for m in neighbors)
        answer = await self.chat.chat(RECONCILE_PROMPT.format(fact=fact, existing=existing), max_tokens=1024)
        first = answer.splitlines()[0].strip() if answer else "NOOP"
        verb = first.split()[0].upper() if first.split() else "NOOP"
        target = first.split()[1].strip("`'\"") if len(first.split()) > 1 else ""
        known = {m.name for m in neighbors}

        if verb == "ADD":
            add()
        elif verb == "UPDATE" and target in known:
            mem = self.store.get(target)
            merged = answer.split("---", 1)[1].strip() if "---" in answer else f"{mem.body}\n\n{fact}"
            self.report.facts_updated += 1
            self.report.decisions.append(f"UPDATE {target}: {fact[:80]}")
            if not self.dry_run:
                mem.body = merged
                if sid not in mem.evidence:
                    mem.evidence.append(sid)
                self.store.save(mem)
        elif verb == "ARCHIVE" and target in known:
            self.report.facts_archived += 1
            self.report.decisions.append(f"ARCHIVE {target} (superseded): {fact[:80]}")
            if not self.dry_run:
                self.store.archive(target)
            add()
        else:
            self.report.facts_noop += 1
            self.report.decisions.append(f"NOOP: {fact[:90]}")

    # -- job 2: project digest ------------------------------------------------------

    async def update_state(self, new_episodes: list[str]) -> None:
        index_path = self.store.root / "MEMORY.md"
        text = self.store.index_text()
        m = re.search(f"{re.escape(DREAM_MARK_START)}(.*?){re.escape(DREAM_MARK_END)}", text, re.DOTALL)
        current = m.group(1).strip() if m else ""

        answer = await self.chat.chat(STATE_PROMPT.format(
            current=current or "(empty)",
            episodes="\n\n".join(new_episodes) or "(none)",
            max_lines=MAX_STATE_LINES,
        ), max_tokens=1024)
        state = "\n".join(answer.splitlines()[:MAX_STATE_LINES]).strip()
        if not state:
            return
        block = f"{DREAM_MARK_START}\n## current state (dream-maintained)\n{state}\n{DREAM_MARK_END}"
        if m:
            text = text[: m.start()] + block + text[m.end():]
        else:
            text = (text.rstrip() + "\n\n" if text.strip() else "# MEMORY\n\n") + block + "\n"
        self.report.state_updated = True
        self.report.decisions.append("MEMORY.md state section rewritten")
        if not self.dry_run:
            index_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic + utf-8: MEMORY.md is the user's hand-curated file. Write a
            # temp then os.replace, so a crash mid-write can't truncate/corrupt
            # it; utf-8 so CJK/emoji don't crash on a non-UTF-8 (Windows) locale.
            tmp = index_path.with_name(index_path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, index_path)

    # -- the pass --------------------------------------------------------------------

    async def run(self, limit: int = 10, index=None) -> DreamReport:
        if not self._acquire_lock():
            raise RuntimeError("another dream is in progress (.dream.lock is fresh)")
        try:
            sessions = self._pending_sessions(limit)
            self.log(f"job 1/4 · consolidate {len(sessions)} session(s)")
            episode_summaries: list[str] = []
            all_facts: list[tuple[str, str]] = []
            for session in sessions:
                got = await self.consolidate(session)
                if got is None:
                    continue
                facts, note = got
                episode_summaries.append(note)
                all_facts.extend((f, session["session_id"]) for f in facts)

            if episode_summaries:
                from rockycode.dream.proposals import draft_proposals, draft_routine_proposals
                sids = [s["session_id"] for s in sessions]
                await draft_proposals(self, episode_summaries, sids)
                await draft_routine_proposals(self, episode_summaries, sids)

            # The skill watch ticks every pass (free, code-only): a dream
            # skill nobody loads eventually asks to be retired.
            from rockycode.dream.proposals import watch_skills
            watch_skills(self)

            self.log(f"job 2/4 · reconcile {len(all_facts)} fact(s)")
            for fact, sid in all_facts:
                await self.reconcile(fact, sid, index=index)

            if episode_summaries:
                self.log("job 3/4 · rewrite project state")
                await self.update_state(episode_summaries)

            if index is not None and not self.dry_run:
                self.log("job 4/4 · re-embed changed memories")
                try:
                    self.report.reindexed = await index.reindex()
                except Exception:  # noqa: BLE001 — embedding refresh is best-effort
                    pass
            return self.report
        finally:
            self._release_lock()
