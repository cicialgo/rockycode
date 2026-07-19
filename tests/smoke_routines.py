"""Routines core (self-evolve phase 2, slice 2) — declaration, due, lease. Pure.

Contracts under test:
  - toml roundtrip: declaration survives save/load; unknown keys ignored;
    bad cadence degrades to daily; disabled routines hidden from list().
  - due(): never-ran = due; daily/weekly cadence; missed runs never stack.
  - the auto LEASE (locked design): grant clamps to MAX_LEASE_DAYS, expires
    by deadline OR by lease budget (whichever first), renewal resets the
    lease odometer, revoke drops back to click-to-run.
  - record_run: odometer moves, history is bounded, project filter works.
"""
import os
import tempfile
import time
from pathlib import Path

os.environ["ROCKYCODE_HOME"] = tempfile.mkdtemp(prefix="rockyhome-")

from rockycode.routines import CADENCES, MAX_LEASE_DAYS, Routine, RoutineStore, routines_dir

DAY = 86_400.0


def make(name="arxiv-digest", **kw):
    defaults = dict(description="daily arxiv sweep", cadence="daily",
                    prompt="sweep arxiv per the skill", workdir="/tmp/w",
                    project_id="proj-1", network=True,
                    tools=["web_fetch", "write_file"], budget_run=0.05)
    defaults.update(kw)
    return Routine(name=name, **defaults)


def main():
    store = RoutineStore()
    now = time.time()

    # --- roundtrip ---
    store.save(make(), skill_md="# arxiv-digest\n\n## steps\n- sweep")
    r = store.load("arxiv-digest")
    assert r is not None and r.network is True and r.tools == ["web_fetch", "write_file"]
    assert r.budget_run == 0.05 and (routines_dir() / "arxiv-digest" / "SKILL.md").exists()
    # unknown keys ignored, bad cadence degrades
    (routines_dir() / "odd" ).mkdir(parents=True)
    (routines_dir() / "odd" / "routine.toml").write_text(
        'name = "odd"\ncadence = "hourly"\nmystery = 3\n', encoding="utf-8")
    odd = store.load("odd")
    assert odd is not None and odd.cadence == "daily", odd
    print("routines: toml roundtrip, lenient load  ✓")

    # --- due: never-ran is due; cadence gates; missed runs don't stack ---
    assert [x.name for x in store.due(now=now)] == ["arxiv-digest", "odd"]
    store.record_run(r, session_id="s1", cost=0.02, status="ok", now=now)
    assert all(x.name != "arxiv-digest" for x in store.due(now=now + DAY * 0.5)), "not due yet"
    assert any(x.name == "arxiv-digest" for x in store.due(now=now + DAY * 1.1)), "due after a day"
    # 10 days asleep → still due exactly once (due() returns it once per launch)
    assert sum(x.name == "arxiv-digest" for x in store.due(now=now + DAY * 10)) == 1
    weekly = make(name="deps-bump", cadence="weekly", project_id="proj-2")
    store.save(weekly)
    store.record_run(weekly, session_id="s2", cost=0.0, status="ok", now=now)
    assert all(x.name != "deps-bump" for x in store.due(now=now + DAY * 3))
    assert any(x.name == "deps-bump" for x in store.due(now=now + DAY * 7.5))
    print("routines: due follows cadence, never stacks  ✓")

    # --- project filter: "" = global, else exact match ---
    assert {x.name for x in store.list(project_id="proj-1")} == {"arxiv-digest", "odd"}
    assert "deps-bump" in {x.name for x in store.list(project_id="proj-2")}
    assert "odd" in {x.name for x in store.list(project_id="proj-2")}, "global ('' ) routines show everywhere"
    print("routines: project filter with global routines  ✓")

    # --- the lease: clamped grant, dual expiry, renewal resets, revoke ---
    r = store.grant_lease(r, days=30, budget=0.10)  # asks 30 → clamped to 7
    assert r.lease_deadline <= time.time() + MAX_LEASE_DAYS * DAY + 5, "lease must clamp"
    assert store.lease_active(r)
    assert not store.lease_active(r, now=time.time() + 8 * DAY), "deadline expiry"
    store.record_run(r, session_id="s3", cost=0.06, status="ok")
    assert store.lease_active(r), "0.06 of 0.10 spent — still active"
    store.record_run(r, session_id="s4", cost=0.05, status="ok")
    assert not store.lease_active(r), "0.11 of 0.10 — budget expiry"
    r = store.grant_lease(r, days=7, budget=0.10)   # renewal resets the odometer
    assert store.state(r).lease_spent == 0.0 and store.lease_active(r)
    r = store.revoke_lease(r)
    assert not store.lease_active(r) and store.load("arxiv-digest").auto is False
    print("routines: lease clamps, expires by deadline OR budget, renews clean  ✓")

    # --- odometer: bounded history, persisted ---
    for i in range(60):
        store.record_run(r, session_id=f"b{i}", cost=0.001, status="ok")
    st = store.state(r)
    assert len(st.runs) == 50 and st.runs[-1]["sid"] == "b59", "history must stay bounded"
    # disabled routines vanish from list/due
    r.enabled = False
    store.save(r)
    assert all(x.name != "arxiv-digest" for x in store.list())
    print("routines: bounded odometer, disabled means gone  ✓")

    print("ROUTINES SMOKE OK — trust is a lease, not a switch. amaze!")


main()
