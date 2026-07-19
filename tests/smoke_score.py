"""score._find_report: match run_id EXACTLY (not a substring) and take the
newest matching report. No swebench / docker — pure file matching."""
import json
import os
import tempfile
from pathlib import Path

from rockycode import score

os.chdir(tempfile.mkdtemp(prefix="rockyscore-"))


def _report(resolved):
    return json.dumps({"resolved_ids": resolved})


# a 'v12' report must NOT be returned when scoring 'v1' (the old loose glob did)
Path("model.v12.json").write_text(_report(["x"]))
assert score._find_report("v1") is None, "substring-matched v1 inside v12"

# exact delimited match works
Path("model.v1.json").write_text(_report(["a"]))
os.utime("model.v1.json", (1000, 1000))  # older
assert score._find_report("v1") == {"resolved_ids": ["a"]}

# a newer report for the SAME run_id wins (a re-run must not reuse a stale one)
Path("other.v1.json").write_text(_report(["a", "b"]))
os.utime("other.v1.json", (2000, 2000))  # newer
assert score._find_report("v1") == {"resolved_ids": ["a", "b"]}, score._find_report("v1")

print("SCORE SMOKE OK — exact run_id match + newest wins. amaze!")
