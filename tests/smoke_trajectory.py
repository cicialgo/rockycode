"""Trajectory logging is best-effort: it writes utf-8 normally, and an
unwritable location degrades to a no-op instead of crashing the session.

Pure — no network, no API. The unwritable case puts a FILE where a parent
directory should be, so `mkdir(parents=True)` fails deterministically for any
user (a chmod-0500 dir wouldn't stop root).
"""
import tempfile
from pathlib import Path

from rockycode.engine.trajectory import TrajectoryLogger


def main():
    # --- normal: a writable dir logs, including raw utf-8 (CJK / emoji) ---
    d = Path(tempfile.mkdtemp(prefix="rockytraj-"))
    log = TrajectoryLogger({"greeting": "there"}, directory=d)
    assert log.path is not None and log.path.exists(), "meta line not written"
    assert log.disabled_reason is None
    log.message({"role": "user", "content": "汉字 😀"})
    log.feedback({"mood": "good", "text": "ok", "local_only": True})
    text = log.path.read_text(encoding="utf-8")
    assert "there" in text and "汉字" in text and "😀" in text, text
    assert '"feedback"' in text and '"local_only"' in text, text
    print("trajectory: writable dir logs utf-8 (CJK + emoji)  ✓")

    # --- unwritable: a file stands where a parent dir should be -> mkdir raises,
    #     and the logger disables itself instead of taking the session down ---
    blocker = Path(tempfile.mkdtemp(prefix="rockytraj-")) / "afile"
    blocker.write_text("x")  # a FILE, not a dir
    log2 = TrajectoryLogger({"m": 1}, directory=blocker / "sub")
    assert log2.path is None, "logging should be disabled on an unwritable location"
    assert log2.disabled_reason, "should record why it disabled"
    # these must NOT raise — logging is off, the turn goes on
    log2.message({"role": "user", "content": "still alive"})
    log2.compaction({"n": 1})
    log2.outcome({"ok": True})
    log2.feedback({"mood": "meh"})
    print("trajectory: unwritable location degrades gracefully, no crash  ✓")

    print("TRAJECTORY SMOKE OK — logging is best-effort. amaze!")


main()
