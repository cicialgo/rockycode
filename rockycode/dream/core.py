"""The dream pass (M2): offline memory consolidation on a local Ollama model.

Design: docs/memory-dream.md §3. Jobs in this milestone:

  1. episode digestion — each un-dreamed trajectory becomes an episode note
     (task / outcome / what worked / what failed), failures recorded while
     fresh; durable facts extracted as candidates
  2. project digest — the dream owns ONE marked section of MEMORY.md
     (between dream:state markers); the rest stays hand-curated
  3. reconciliation — each candidate fact vs its nearest existing memories:
     NOOP / ADD / UPDATE / ARCHIVE (archive never deletes — M0 rule)
  6. re-embed — sync index.db when something changed

Idle trigger, decay, and skill promotion are M3. Everything auto-applies
(user decision 2026-06-12) — `--dry-run` previews instead.

The model runs through Ollama's NATIVE /api/chat with `think: false`: the
OpenAI-compat endpoint ignores every thinking switch for qwen3.5 (measured
2026-06-13: 2 completion tokens vs ~100+ for a bare "ok").

"Already dreamed" is derived from the files, not a state db: a session id
listed in any episode's `evidence:` has been digested. Delete the episode
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

import httpx

from rockycode.memory.store import Memory, MemoryStore, _slugify

OLLAMA_URL = os.getenv("ROCKYCODE_OLLAMA_URL", "http://localhost:11434")
# 2b on purpose (bake-off 2026-07-17, n=5): every qwen3.5 size follows the
# JSON contracts fine (think:false verified working on native /api/chat,
# ollama 0.24.0) — the sizes differ in JUDGMENT, not format. 4b/9b decline
# ("[]"/null) on evidence 2b happily mines. The pipeline wants the eager
# miner: precision comes later (hot needs reinforcement, installs need a
# human), while a declined pattern is lost forever. Env-overridable.
DREAM_MODEL = os.getenv("ROCKYCODE_DREAM_MODEL", "qwen3.5:2b")

TRAJECTORY_DIRNAME = Path(".rockycode") / "trajectories"
LOCK_STALE_S = 3600
MIN_SESSION_MESSAGES = 4          # meta+system+user only → nothing to learn
MAX_TRANSCRIPT_CHARS = 6_000
MAX_STATE_LINES = 25

DREAM_MARK_START = "<!-- dream:state -->"
DREAM_MARK_END = "<!-- /dream:state -->"

DIGEST_PROMPT = """\
You are consolidating a coding agent's work session into a memory note.

SESSION TRANSCRIPT (condensed):
{transcript}

Write exactly these markdown sections, nothing outside them:
## task
One or two sentences: what the session tried to accomplish.
## outcome
success / partial / failed — plus one sentence of evidence.
## worked
Bullets: approaches or commands that worked. Write "- none" if none.
## failed
Bullets: approaches that failed or wasted time, so they are not retried. "- none" if none.
## facts
Bullets: durable project facts worth remembering across sessions (paths,
commands, configuration gotchas). Each bullet ONE standalone line. "- none" if none.
## importance
One integer 1-10: how much future sessions benefit from this note.
"""

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


class OllamaChat:
    """Minimal native /api/chat client — think:false actually works here."""

    def __init__(self, model: str = DREAM_MODEL, base_url: str = OLLAMA_URL) -> None:
        self.model = model
        self.base_url = base_url

    async def chat(self, prompt: str, max_tokens: int = 2048) -> str:
        async with httpx.AsyncClient(timeout=300.0) as http:
            resp = await http.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "think": False,
                    "stream": False,
                    "options": {"num_predict": max_tokens},
                },
            )
            resp.raise_for_status()
            return (resp.json().get("message") or {}).get("content", "").strip()


@dataclass
class DreamReport:
    sessions_digested: int = 0
    sessions_judged: int = 0
    sessions_skipped: int = 0
    facts_added: int = 0
    facts_updated: int = 0
    facts_archived: int = 0
    facts_noop: int = 0
    weaknesses_added: int = 0
    weaknesses_reinforced: int = 0
    proposals_drafted: int = 0
    state_updated: bool = False
    reindexed: Optional[tuple[int, int, int]] = None
    decisions: list[str] = field(default_factory=list)


# ---- trajectory condensing ----------------------------------------------------

def load_session(path: Path) -> Optional[dict]:
    meta, messages, outcome, heuristic, feedback = {}, [], None, None, None
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
                outcome = rec.get("data", {})  # last wins: judge > heuristic
                if outcome.get("source") == "heuristic":
                    heuristic = outcome  # kept separately — mining needs the counters
            elif rec.get("kind") == "feedback":
                feedback = rec.get("data", {})  # the exit sheet — LOCAL ONLY
    except OSError:
        return None
    if len(messages) < MIN_SESSION_MESSAGES - 1:
        return None
    if not any(m.get("role") == "assistant" for m in messages):
        return None
    return {
        "session_id": path.stem, "path": str(path), "meta": meta,
        "messages": messages, "outcome": outcome, "heuristic": heuristic,
        "feedback": feedback,
    }


def condense(session: dict, *, feedback: bool = False) -> str:
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
    # The exit sheet is opt-in per CALLER, not per session: its trajectory
    # record promises "never sent to the model provider", so only a condense
    # destined for the LOCAL Ollama dream may pass feedback=True. A future
    # cloud judge must keep the default.
    if feedback and session.get("feedback"):
        fb = session["feedback"]
        note = f" — {fb.get('text', '')}" if fb.get("text") else ""
        parts.append(f"[user exit-feedback] mood={fb.get('mood')}{note}"[:300])
    text = "\n".join(parts)
    if len(text) > MAX_TRANSCRIPT_CHARS:
        half = MAX_TRANSCRIPT_CHARS // 2
        text = f"{text[:half]}\n… [middle of session omitted] …\n{text[-half:]}"
    return text


def parse_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        m = re.match(r"^##+\s*(\w+)", line.strip())
        if m:
            current = m.group(1).lower()
            sections[current] = ""
        elif current:
            sections[current] += line + "\n"
    return {k: v.strip() for k, v in sections.items()}


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


def parse_bullets(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.strip().lstrip("-*").strip()
        if line and line.lower() not in ("none", "none.", "无"):
            out.append(line)
    return out


# ---- the runner ----------------------------------------------------------------

class DreamRunner:
    def __init__(
        self,
        workdir: Path,
        *,
        model: str = DREAM_MODEL,
        chat: Optional[OllamaChat] = None,
        dry_run: bool = False,
        log: Callable[[str], None] = lambda s: None,
        exclude: Optional[set[str]] = None,
        judge=None,
    ) -> None:
        self.workdir = workdir
        self.store = MemoryStore.for_workdir(workdir)
        self.chat = chat or OllamaChat(model=model)
        self.dry_run = dry_run
        self.log = log
        # Session ids to leave alone this pass — the TUI's launch catch-up
        # excludes the LIVE session (its outcome record doesn't exist yet).
        self.exclude = exclude or set()
        # Optional TranscriptJudge (dream/judge.py). Runs BEFORE digestion so
        # the episode note sees the judged outcome; skipped entirely on
        # --dry-run (a preview must be free — the judge bills a cloud call).
        self.judge = judge
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
            # global store → only digest THIS project's sessions (no cross-
            # project memory contamination).
            if session["meta"].get("project_id") != pid:
                continue
            sessions.append(session)
            if len(sessions) >= limit:
                break
        return sessions

    # -- job 1: episode digestion ------------------------------------------------

    async def digest(self, session: dict) -> tuple[list[str], str, Optional[str]]:
        """Returns (candidate facts, note text for the state prompt, and a
        failure note for weakness mining — None when the session shows no
        failure signals)."""
        from rockycode.dream.mining import failure_note

        sid = session["session_id"]
        answer = await self.chat.chat(
            DIGEST_PROMPT.format(transcript=condense(session, feedback=True))
        )
        sections = parse_sections(answer)
        task = (sections.get("task") or "(unknown task)").strip()
        try:
            importance = max(1, min(10, int(re.search(r"\d+", sections.get("importance", "5")).group())))
        except (AttributeError, ValueError):
            importance = 5

        body = "\n\n".join(
            f"## {key}\n{sections.get(key, '- none')}" for key in ("task", "outcome", "worked", "failed")
        )
        episode = Memory(
            name=f"ep-{sid[:15]}",
            type="episode",
            description=task.splitlines()[0][:150],
            importance=importance,
            origin="dream",
            evidence=[sid],
            body=body,
        )
        self.report.decisions.append(f"episode ep-{sid[:15]}: {episode.description}")
        if not self.dry_run:
            self.store.save(episode)
        self.report.sessions_digested += 1
        return (
            parse_bullets(sections.get("facts", "")),
            f"### {episode.description}\n{body[:600]}",
            failure_note(session, sections),
        )

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
            if self.judge is not None and not self.dry_run and sessions:
                self.log(f"judge pass · grading {len(sessions)} transcript(s) (cloud)")
                from rockycode.engine.trajectory import append_record
                for session in sessions:
                    graded = await self.judge.grade(session)
                    if graded is None:
                        continue  # gated out or call failed — heuristic stands
                    self.report.sessions_judged += 1
                    self.report.decisions.append(
                        f"JUDGE {session['session_id'][:15]}: score {graded['score']}"
                    )
                    append_record(Path(session["path"]), "outcome", graded)
                    session["outcome"] = graded  # the digest below sees the verdict
            self.log(f"job 1/4 · digest {len(sessions)} session(s)")
            episode_summaries: list[str] = []
            all_facts: list[tuple[str, str]] = []
            failure_notes: list[tuple[str, str]] = []
            for session in sessions:
                facts, note, fnote = await self.digest(session)
                episode_summaries.append(note)
                all_facts.extend((f, session["session_id"]) for f in facts)
                if fnote:
                    failure_notes.append((session["session_id"], fnote))

            if failure_notes:
                self.log(f"weakness mining · {len(failure_notes)} failure note(s)")
                from rockycode.dream.mining import mine_weaknesses
                await mine_weaknesses(self, failure_notes)

            if sessions:
                from rockycode.dream.proposals import draft_proposals, draft_routine_proposals
                sids = [s["session_id"] for s in sessions]
                await draft_proposals(self, episode_summaries, sids)
                await draft_routine_proposals(self, episode_summaries, sids)

            self.log(f"job 2/4 · reconcile {len(all_facts)} fact(s)")
            for fact, sid in all_facts:
                await self.reconcile(fact, sid, index=index)

            if sessions:
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
