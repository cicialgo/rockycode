"""Catch-up run state — bookmarks, a reported-items ledger, and run records
for an unattended "what changed since last time" brief (a routine).

Pure, file-backed, no network. The routine's playbook (SKILL.md) reads and
writes ONE directory through this module (or by hand, same format):

  bookmarks.json — {source: newest_item_timestamp}. The next run reads from
                   the bookmark, never a fixed "last 24h" window, so a late
                   run leaves no gap and an early one repeats nothing.
  ledger.jsonl   — one line per reported item: {day, source, id, status}.
                   An item already reported and still open is carried as
                   "still waiting, day N", not re-reported.
  runs/<day>.json — the run record: posting → posted (with message id) |
                   maybe_posted | held. Bookmarks and the ledger move ONLY
                   after a confirmed post; an unclear post changes nothing,
                   so nothing is lost and nothing is sent twice.

A source that failed to read keeps its bookmark (unreadable ≠ quiet day).
Design + sources: docs/design/offline-agent-guide-comparison.md.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

POSTING, POSTED, MAYBE_POSTED, HELD = "posting", "posted", "maybe_posted", "held"
_STATUSES = (POSTING, POSTED, MAYBE_POSTED, HELD)


@dataclass
class Item:
    source: str          # "github", "slack:C0123"
    id: str              # stable id: PR number, Slack ts
    line: str = ""       # what the brief says
    status: str = "open"  # open | closed
    ts: str = ""         # source timestamp (ISO-8601) — drives the bookmark


@dataclass
class Plan:
    new: list[Item] = field(default_factory=list)
    carried: list[tuple[Item, int]] = field(default_factory=list)  # (item, days waiting)
    dropped: list[Item] = field(default_factory=list)              # closed since reported


def _day_n(first: str, today: str) -> int:
    from datetime import date
    try:
        return (date.fromisoformat(today) - date.fromisoformat(first)).days + 1
    except ValueError:
        return 1


class CatchupState:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # -- bookmarks -------------------------------------------------------------
    def bookmarks(self) -> dict[str, str]:
        try:
            data = json.loads((self.root / "bookmarks.json").read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    # -- ledger ----------------------------------------------------------------
    def ledger(self) -> dict[tuple[str, str], dict]:
        out: dict[tuple[str, str], dict] = {}
        try:
            text = (self.root / "ledger.jsonl").read_text(encoding="utf-8")
        except OSError:
            return out
        for raw in text.splitlines():
            try:
                row = json.loads(raw)
                key = (str(row["source"]), str(row["id"]))
            except (ValueError, KeyError, TypeError):
                continue  # a torn line never poisons the rest
            prev = out.get(key)
            row.setdefault("first", prev["first"] if prev else row.get("day", ""))
            out[key] = row
        return out

    def plan(self, items: Iterable[Item], today: str) -> Plan:
        """Dedupe against the ledger: new → report, open+known → carry with
        its day count, closed+known → drop silently, closed+unknown → ignore."""
        led = self.ledger()
        p = Plan()
        for it in items:
            known = led.get((it.source, it.id))
            if known is None:
                if it.status != "closed":
                    p.new.append(it)
            elif it.status == "closed":
                if known.get("status") != "closed":
                    p.dropped.append(it)
            else:
                p.carried.append((it, _day_n(known.get("first", today), today)))
        return p

    # -- run records -----------------------------------------------------------
    def run_record(self, day: str) -> Optional[dict]:
        try:
            return json.loads((self.root / "runs" / f"{day}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def already_posted(self, day: str) -> bool:
        rec = self.run_record(day)
        return bool(rec) and rec.get("status") == POSTED

    def mark(self, day: str, status: str, **extra) -> dict:
        if status not in _STATUSES:
            raise ValueError(f"status must be one of {_STATUSES}, got {status!r}")
        rec = {**(self.run_record(day) or {}), **extra, "day": day, "status": status}
        self._write(self.root / "runs" / f"{day}.json", json.dumps(rec, indent=2))
        return rec

    def commit(self, day: str, plan: Plan, *, message_id: str,
               read_ok: dict[str, bool], newest: dict[str, str]) -> dict:
        """Call ONLY after the destination confirmed the post (e.g. Slack
        ok:true + ts). Moves the ledger and the bookmarks of sources that
        were actually read; a failed source keeps its old bookmark."""
        if not message_id:
            raise ValueError("commit needs the confirmed message id; use mark(MAYBE_POSTED)")
        rows = [{"day": day, "source": it.source, "id": it.id,
                 "status": it.status, "line": it.line} for it in plan.new]
        rows += [{"day": day, "source": it.source, "id": it.id,
                  "status": it.status, "line": it.line} for it, _ in plan.carried]
        rows += [{"day": day, "source": it.source, "id": it.id, "status": "closed",
                  "line": it.line} for it in plan.dropped]
        if rows:
            self.root.mkdir(parents=True, exist_ok=True)
            with open(self.root / "ledger.jsonl", "a", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        marks = self.bookmarks()
        for src, ok in read_ok.items():
            if ok and newest.get(src):
                marks[src] = max(marks.get(src, ""), newest[src])
        self._write(self.root / "bookmarks.json", json.dumps(marks, indent=2, sort_keys=True))
        unread = sorted(s for s, ok in read_ok.items() if not ok)
        return self.mark(day, POSTED, message_id=message_id, unreadable=unread)

    @staticmethod
    def _write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)  # atomic: a crash never leaves half a bookmark file
