"""Single writer for desktop records. No model credentials live here."""
import json
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        self.path = directory / "workspace.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise RuntimeError("Unsupported workspace database version")
        self.db.execute("CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, data TEXT NOT NULL, history TEXT NOT NULL)")
        self.db.execute("PRAGMA user_version=1")
        self.db.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def all(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM threads")]

    def history(self, thread_id):
        row = self.db.execute("SELECT history FROM threads WHERE id=?", (thread_id,)).fetchone()
        return json.loads(row[0]) if row else []

    def save(self, thread, history=None):
        if history is None:
            history = self.history(thread["id"])
        with self.db:
            self.db.execute("INSERT INTO threads VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data, history=excluded.history", (thread["id"], json.dumps(thread, ensure_ascii=False), json.dumps(history, ensure_ascii=False)))

    def delete(self, thread_id):
        with self.db:
            self.db.execute("DELETE FROM threads WHERE id=?", (thread_id,))

    def close(self):
        self.db.close()
