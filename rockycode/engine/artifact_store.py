"""Project-local artifact versions. SQLite commits publish immutable HTML files.

A failed/conflicting write never replaces the previous revision. Preview URLs,
server tokens and browser connection counts are deliberately not persisted.
"""
from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path


class ArtifactStore:
    def __init__(self, workdir: Path) -> None:
        self.root = workdir.resolve() / ".rockycode" / "artifacts"
        self.db = self.root / "index.sqlite3"

    def _check(self, path: Path) -> None:
        # Metadata must not turn a project file into an arbitrary host path.
        for part in (self.root.parent, self.root, *path.relative_to(self.root).parents):
            candidate = part if part.is_absolute() else self.root / part
            if candidate.is_symlink():
                raise ValueError("artifact storage cannot contain symlinks")
        if path.is_symlink():
            raise ValueError("artifact storage cannot contain symlinks")
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("artifact path leaves project storage")

    def _connect(self) -> sqlite3.Connection:
        self._check(self.db)
        self.root.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db, timeout=5)
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
                revision INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revisions (
                artifact_id TEXT NOT NULL, revision INTEGER NOT NULL, title TEXT NOT NULL,
                theme TEXT NOT NULL, layout TEXT NOT NULL, session_id TEXT NOT NULL,
                created REAL NOT NULL, PRIMARY KEY (artifact_id, revision)
            );
            CREATE TABLE IF NOT EXISTS session_sources (
                session_id TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY(session_id, source)
            );
        """)
        return con

    def path(self, artifact_id: str, revision: int, name: str) -> Path:
        if not re.fullmatch(r"art_[0-9a-f]{16}", artifact_id):
            raise ValueError("invalid artifact ID")
        if not isinstance(revision, int) or revision < 1:
            raise ValueError("invalid artifact revision")
        if not name or len(name) > 80 or any(c not in "._-" and not c.isalnum() for c in name):
            raise ValueError("invalid artifact name")
        p = self.root / "versions" / artifact_id / str(revision) / f"{name}.html"
        self._check(p)
        return p

    def records(self) -> list[dict]:
        if not self.db.exists():
            return []
        con = self._connect()
        try:
            rows = con.execute("""SELECT a.*, r.theme, r.layout FROM artifacts a
                JOIN revisions r ON a.id=r.artifact_id AND a.revision=r.revision
                ORDER BY a.updated DESC, a.id""").fetchall()
            result = []
            for row in rows:
                d = dict(row)
                d['path'] = self.path(d['id'], d['revision'], d['name'])
                d['sessions'] = tuple(r[0] for r in con.execute(
                    "SELECT DISTINCT session_id FROM revisions WHERE artifact_id=?", (d['id'],)))
                result.append(d)
            return result
        finally:
            con.close()

    def history(self, artifact_id: str) -> list[dict]:
        if not self.db.exists():
            return []
        con = self._connect()
        try:
            rows = con.execute("""SELECT r.*, a.name FROM revisions r JOIN artifacts a
                ON a.id=r.artifact_id WHERE a.id=? ORDER BY r.revision DESC""", (artifact_id,)).fetchall()
            return [{**dict(r), 'path': self.path(artifact_id, r['revision'], r['name'])} for r in rows]
        finally:
            con.close()

    def resume_from(self, session_id: str, source: str) -> None:
        if not self.db.exists():
            return
        con = self._connect()
        try:
            with con:
                con.execute("INSERT OR IGNORE INTO session_sources VALUES (?,?)", (session_id, source))
        finally:
            con.close()

    def session_scope(self, session_id: str) -> set[str]:
        seen = {session_id}
        if not self.db.exists():
            return seen
        con = self._connect()
        try:
            pending = [session_id]
            while pending:
                for row in con.execute("SELECT source FROM session_sources WHERE session_id=?", (pending.pop(),)):
                    if row[0] not in seen:
                        seen.add(row[0])
                        pending.append(row[0])
            return seen
        finally:
            con.close()

    def save(self, *, title: str, document: str, session_id: str, theme: str, layout: str,
             artifact_id: str | None = None, base_revision: int | None = None) -> str:
        from rockycode.engine.artifact import _safe_filename

        con = self._connect()
        written = None
        temporary = None
        try:
            con.execute("BEGIN IMMEDIATE")
            now = time.time()
            if artifact_id:
                old = con.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
                if old is None:
                    raise ValueError("unknown artifact_id; select an existing artifact or omit the ID to create")
                if base_revision != old['revision']:
                    raise ValueError(f"artifact changed: current revision is {old['revision']}; read it before updating")
                revision, name = old['revision'] + 1, old['name']
            else:
                if base_revision is not None:
                    raise ValueError("base_revision requires artifact_id")
                artifact_id = 'art_' + uuid.uuid4().hex[:16]
                name = _safe_filename(title)[:50] + '--' + artifact_id
                revision = 1
            path = self.path(artifact_id, revision, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            # The database write lock excludes competing publishers. The next
            # revision is not referenced yet: replace an orphan left by a crash
            # between the file write and DB commit, never a published version.
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                             dir=path.parent, delete=False) as f:
                temporary = Path(f.name)
                f.write(document)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, path)
            temporary = None
            written = path
            con.execute("INSERT INTO revisions VALUES (?,?,?,?,?,?,?)",
                        (artifact_id, revision, title, theme, layout, session_id, now))
            if revision == 1:
                con.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?)",
                            (artifact_id, name, title, revision, now, now))
            else:
                con.execute("UPDATE artifacts SET title=?,revision=?,updated=? WHERE id=?",
                            (title, revision, now, artifact_id))
            con.commit()
            return artifact_id
        except BaseException:
            con.rollback()
            if written is not None:
                written.unlink(missing_ok=True)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise
        finally:
            con.close()

    def import_legacy(self, path: Path, session_id: str) -> str:
        """Explicitly import one old top-level HTML; leave the original alone."""
        # The root is resolved; the caller's spelling of the same folder may not be
        # (macOS temp dirs sit behind /var → /private/var). Compare folders, then
        # check the file under the root's own spelling.
        if path.parent.resolve() != self.root or path.suffix.lower() != '.html':
            raise ValueError('select a legacy HTML file from the artifact directory')
        path = self.root / path.name
        self._check(path)
        if not path.is_file():
            raise ValueError('select a legacy HTML file from the artifact directory')
        if path.stat().st_size > 2_000_000:
            raise ValueError('legacy artifact is too large')
        return self.save(title=path.stem, document=path.read_text(encoding='utf-8'),
                         session_id=session_id, theme='none', layout='document')
