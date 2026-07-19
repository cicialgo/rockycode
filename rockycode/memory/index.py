"""Semantic memory index: Ollama embeddings in sqlite-vec, FTS5 keyword hybrid.

`index.db` is a disposable cache derived from the markdown files — delete it
any time, the next search rebuilds it. The files stay the truth (M0 rule).

Two embedding SPACES, every document in both (user setup, verified live
2026-06-12 — including the cross-lingual case):
- "en" space: nomic-embed-text (768d) — the precision space for
  English↔English. REQUIRES `search_document:` / `search_query:` task
  prefixes; without them quality silently degrades.
- "zh" space: qwen3-embedding:0.6b (1024d) — multilingual, so a Chinese
  query matches an English memory here and vice versa.

Storing docs in one language-routed table would break exactly the user's
real pattern (asking in Chinese about English code facts), so language
detection only steers query-side weights: English queries trust the nomic
space first; Chinese queries search only the qwen space (nomic cannot embed
CJK meaningfully). FTS5 keyword hits join the rank fusion in both cases.

Degradation ladder — search never throws at the caller:
  Ollama down            → FTS5 keyword search only
  sqlite-vec won't load  → IndexUnavailable at construction; callers fall
                           back to the store's substring search (M0 path)
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sqlite3
import struct
from pathlib import Path
from typing import Optional

from rockycode.memory.store import Memory, MemoryStore

OLLAMA_URL = os.getenv("ROCKYCODE_OLLAMA_URL", "http://localhost:11434")

# space → (model, dimensions, document prefix, query prefix)
EMBED_MODELS = {
    "en": ("nomic-embed-text", 768, "search_document: ", "search_query: "),
    "zh": ("qwen3-embedding:0.6b", 1024, "", ""),
}
# query language → {space: RRF weight}; FTS5 weight applies to both
SPACE_WEIGHTS = {
    "en": {"en": 1.0, "zh": 0.8},
    "zh": {"zh": 1.0},  # no nomic for CJK queries
}
FTS_WEIGHT = 0.8
MAX_EMBED_CHARS = 4_000
RRF_K = 60  # standard reciprocal-rank-fusion constant

_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")


class IndexUnavailable(RuntimeError):
    """sqlite-vec could not be loaded; semantic search is off."""


def detect_lang(text: str) -> str:
    return "zh" if len(_CJK.findall(text[:2000])) >= 2 else "en"


def _fts_norm(text: str) -> str:
    """Space out CJK characters so FTS5's unicode61 tokenizer can index them
    individually — otherwise a run like 界面颜色只用十六进制 is one opaque
    token and 颜色 never matches. Queries phrase-match adjacent chars."""
    return _CJK.sub(lambda m: f" {m.group(0)} ", text)


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _doc_text(mem: Memory) -> str:
    return f"{mem.name}\n{mem.description}\n{mem.body}"[:MAX_EMBED_CHARS]


def _doc_hash(mem: Memory) -> str:
    return hashlib.sha256(_doc_text(mem).encode()).hexdigest()[:16]


class MemoryIndex:
    def __init__(
        self,
        store: MemoryStore,
        db_path: Optional[Path] = None,
        client=None,  # AsyncOpenAI-compatible; tests inject a fake
    ) -> None:
        self.store = store
        self.db_path = db_path or (store.root / ".." / "index.db").resolve()
        if client is None:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(base_url=f"{OLLAMA_URL}/v1", api_key="ollama", max_retries=0, timeout=60.0)
        self.client = client
        self._conn: Optional[sqlite3.Connection] = None

    def conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        try:
            import sqlite_vec
        except ImportError as e:
            raise IndexUnavailable(f"sqlite-vec not installed: {e}") from e
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except (AttributeError, sqlite3.OperationalError) as e:
            conn.close()
            raise IndexUnavailable(f"could not load sqlite-vec extension: {e}") from e
        conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS mem(
                name TEXT PRIMARY KEY, lang TEXT, hash TEXT, description TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_en USING vec0(emb float[{EMBED_MODELS['en'][1]}]);
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_zh USING vec0(emb float[{EMBED_MODELS['zh'][1]}]);
            CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(name, description, body);
            """
        )
        self._conn = conn
        return conn

    async def _embed(self, lang: str, texts: list[str], *, query: bool = False) -> list[list[float]]:
        model, _, doc_prefix, query_prefix = EMBED_MODELS[lang]
        prefix = query_prefix if query else doc_prefix
        resp = await self.client.embeddings.create(model=model, input=[prefix + t for t in texts])
        return [d.embedding for d in resp.data]

    def _rowid(self, conn: sqlite3.Connection, name: str) -> Optional[int]:
        row = conn.execute("SELECT rowid FROM mem WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None

    def _remove(self, conn: sqlite3.Connection, name: str) -> None:
        rowid = self._rowid(conn, name)
        if rowid is None:
            return
        for table in ("vec_en", "vec_zh", "fts", "mem"):
            conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (rowid,))

    async def reindex(self, force: bool = False) -> tuple[int, int, int]:
        """Sync index.db with the markdown files. Returns (indexed, kept, removed).

        Hash comparison makes the no-change case a few milliseconds, so this
        runs before every search — the index is always fresh, no manual step.
        """
        conn = self.conn()
        memories = [m for m in self.store.load_all() if m.status == "active"]
        seen = {m.name for m in memories}

        stale = [
            name for (name,) in conn.execute("SELECT name FROM mem").fetchall()
            if name not in seen
        ]
        for name in stale:
            self._remove(conn, name)

        pending: list[Memory] = []
        kept = 0
        for mem in memories:
            row = conn.execute("SELECT hash FROM mem WHERE name = ?", (mem.name,)).fetchone()
            if not force and row is not None and row[0] == _doc_hash(mem):
                kept += 1
                continue
            pending.append(mem)

        if pending:
            texts = [_doc_text(m) for m in pending]
            # every doc goes into BOTH spaces — cross-lingual recall depends on it
            vectors = {space: await self._embed(space, texts) for space in EMBED_MODELS}
            for i, mem in enumerate(pending):
                self._remove(conn, mem.name)
                cur = conn.execute(
                    "INSERT INTO mem(name, lang, hash, description) VALUES (?, ?, ?, ?)",
                    (mem.name, detect_lang(_doc_text(mem)), _doc_hash(mem), mem.description),
                )
                rowid = cur.lastrowid
                for space in EMBED_MODELS:
                    conn.execute(
                        f"INSERT INTO vec_{space}(rowid, emb) VALUES (?, ?)",
                        (rowid, _pack(vectors[space][i])),
                    )
                conn.execute(
                    "INSERT INTO fts(rowid, name, description, body) VALUES (?, ?, ?, ?)",
                    (rowid, mem.name, _fts_norm(mem.description), _fts_norm(mem.body)),
                )
        conn.commit()
        return len(pending), kept, len(stale)

    def _fts_names(self, query: str, k: int) -> list[str]:
        # CJK terms become phrase queries over their spaced-out chars
        # ("颜色" → '"颜 色"'), matching how _fts_norm indexed them.
        terms = " OR ".join(
            f'"{" ".join(_fts_norm(t).split())}"' for t in re.findall(r"\w+", query)[:12]
        )
        if not terms:
            return []
        try:
            rows = self.conn().execute(
                "SELECT m.name FROM fts JOIN mem m ON m.rowid = fts.rowid "
                "WHERE fts MATCH ? ORDER BY rank LIMIT ?",
                (terms, k),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [r[0] for r in rows]

    async def search(self, query: str, k: int = 5) -> list[tuple[Memory, float]]:
        """Hybrid search: both vector tables + FTS5, merged by RRF.

        Ollama being down degrades to keyword-only; this never raises for
        anything but IndexUnavailable (no sqlite-vec at all).
        """
        conn = self.conn()
        try:
            await self.reindex()
        except Exception:  # noqa: BLE001 — embedding refresh is best-effort
            pass

        # Weighted RRF across spaces. Weights are query-language-dependent
        # (SPACE_WEIGHTS): KNN always returns k rows however distant, so the
        # less trustworthy space must not be able to outvote the primary one.
        rankings: list[tuple[float, list[str]]] = []
        for space, weight in SPACE_WEIGHTS[detect_lang(query)].items():
            try:
                qvec = (await self._embed(space, [query[:MAX_EMBED_CHARS]], query=True))[0]
            except Exception:  # noqa: BLE001 — Ollama down → keyword only
                continue
            rows = conn.execute(
                f"SELECT m.name FROM vec_{space} v JOIN mem m ON m.rowid = v.rowid "
                "WHERE v.emb MATCH ? AND k = ? ORDER BY distance",
                (_pack(qvec), k),
            ).fetchall()
            rankings.append((weight, [r[0] for r in rows]))

        rankings.append((FTS_WEIGHT, self._fts_names(query, k)))

        scores: dict[str, float] = {}
        for weight, ranking in rankings:
            for rank, name in enumerate(ranking):
                scores[name] = scores.get(name, 0.0) + weight / (RRF_K + rank)

        out: list[tuple[Memory, float]] = []
        for name, score in sorted(scores.items(), key=lambda kv: -kv[1])[:k]:
            mem = self.store.get(name)
            if mem is not None and mem.status == "active":
                out.append((mem, score))
        return out


def search_sync(index: MemoryIndex, query: str, k: int = 5) -> list[tuple[Memory, float]]:
    return asyncio.run(index.search(query, k))


def reindex_sync(index: MemoryIndex, force: bool = False) -> tuple[int, int, int]:
    return asyncio.run(index.reindex(force=force))
