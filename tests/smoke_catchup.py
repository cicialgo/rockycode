"""Catch-up run state (rockycode/catchup.py). Pure, no network.

Contracts under test:
  - plan(): new open items reported; known+open carried with day count;
    known+closed dropped once; unknown+closed ignored.
  - commit(): only with a confirmed message id; moves ledger + bookmarks of
    sources that were READ; a failed source keeps its bookmark.
  - run records: posting → maybe_posted changes nothing else; already_posted
    guards a second post the same day; torn ledger lines are skipped.
"""
import tempfile
from pathlib import Path

from rockycode.catchup import MAYBE_POSTED, POSTING, CatchupState, Item


def main():
    st = CatchupState(Path(tempfile.mkdtemp(prefix="catchup-")))
    d1 = "2026-10-13"
    items = [Item("github", "481", "review blocked", ts="2026-10-13T01:00:00Z"),
             Item("slack:C1", "1.1", "refund decision", ts="2026-10-13T02:00:00Z"),
             Item("github", "470", "already merged", status="closed")]
    p = st.plan(items, d1)
    assert [i.id for i in p.new] == ["481", "1.1"] and not p.carried and not p.dropped

    # unclear post: nothing moves
    st.mark(d1, POSTING)
    st.mark(d1, MAYBE_POSTED)
    assert st.bookmarks() == {} and st.ledger() == {} and not st.already_posted(d1)

    try:
        st.commit(d1, p, message_id="", read_ok={}, newest={})
        raise AssertionError("commit without message id must fail")
    except ValueError:
        pass

    rec = st.commit(d1, p, message_id="1789.0001",
                    read_ok={"github": True, "slack": True},
                    newest={"github": "2026-10-13T01:00:00Z", "slack": "2026-10-13T02:00:00Z"})
    assert rec["status"] == "posted" and st.already_posted(d1)
    assert st.bookmarks()["slack"] == "2026-10-13T02:00:00Z"

    # day 3: PR still open → carried (day 3); Slack item closed → dropped; slack unreadable
    d3 = "2026-10-15"
    p3 = st.plan([Item("github", "481", "review blocked"),
                  Item("slack:C1", "1.1", "refund decision", status="closed"),
                  Item("github", "490", "new review")], d3)
    assert [i.id for i in p3.new] == ["490"]
    assert [(i.id, n) for i, n in p3.carried] == [("481", 3)]
    assert [i.id for i in p3.dropped] == ["1.1"]
    rec3 = st.commit(d3, p3, message_id="1790.2",
                     read_ok={"github": True, "slack": False},
                     newest={"github": "2026-10-15T00:00:00Z", "slack": "2026-10-15T09:00:00Z"})
    assert st.bookmarks()["slack"] == "2026-10-13T02:00:00Z", "failed read keeps bookmark"
    assert st.bookmarks()["github"] == "2026-10-15T00:00:00Z"
    assert rec3["unreadable"] == ["slack"]
    # closed item is not dropped twice
    assert not st.plan([Item("slack:C1", "1.1", status="closed")], d3).dropped
    # ledger keeps first-reported day across carries
    assert st.ledger()[("github", "481")]["first"] == d1

    # torn line tolerated
    with open(st.root / "ledger.jsonl", "a", encoding="utf-8") as f:
        f.write("{not json\n")
    assert ("github", "490") in st.ledger()
    print("smoke_catchup: ok")


if __name__ == "__main__":
    main()
