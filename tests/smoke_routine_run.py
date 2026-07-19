"""run_routine — exec's headless machinery driven by a routine's contract. No API.

Contracts under test:
  - a run flows through exec (fake client/registry seams), settles the
    odometer (session id, status, cost), and writes last-run.md.
  - the trajectory carries runner="routine" + project_id (dream food) and a
    heuristic outcome record LAST (judge-gateable); the resume picker
    excludes it.
  - the grant envelope: an ungranted risky tool blocks the run (exit 2 →
    status "blocked"); listing the tool in routine.toml grants it.
"""
import asyncio
import json
import os
import tempfile
import types
from pathlib import Path

# Forced fresh: this test writes global routines + trajectories.
os.environ["ROCKYCODE_HOME"] = tempfile.mkdtemp(prefix="rockyhome-")
os.chdir(tempfile.mkdtemp(prefix="rockyrun-"))

from rockycode import session as S
from rockycode.engine.tools import Tool, _fn_schema
from rockycode.routines import Routine, RoutineStore, run_routine

WD = Path.cwd()


class U:
    def model_dump(self):
        return {"prompt_tokens": 40, "completion_tokens": 8}


def chunk(content=None, tool_calls=None, usage=None):
    d = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(usage=usage, choices=[] if content is None and tool_calls is None and usage else [types.SimpleNamespace(delta=d)])


def tc(i, id_, name, args):
    return types.SimpleNamespace(index=i, id=id_, function=types.SimpleNamespace(name=name, arguments=args))


async def stream(chunks):
    for c in chunks:
        yield c


def make_client(turns):
    class FC:
        def __init__(self):
            self.i = 0

        async def create(self, **kw):
            t = turns[min(self.i, len(turns) - 1)]
            self.i += 1
            return stream(t)
    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=FC()))


async def _deploy() -> str:
    return "[exit 0]\ndeployed"


DEPLOY = Tool(name="deploy", schema=_fn_schema("deploy", "d", {}, []), fn=_deploy)  # risky by default


def make_routine(store, name, tools):
    r = Routine(name=name, prompt="do the digest", workdir=str(WD),
                cadence="daily", tools=tools, max_steps=10)
    store.save(r, skill_md="# playbook\n\n- fetch\n- write")
    return store.load(name)


async def main():
    store = RoutineStore()

    # --- happy path: answer-only run ---
    r = make_routine(store, "arxiv-digest", tools=[])
    client = make_client([[chunk(content="digest written. done."), chunk(usage=U())]])
    res = await run_routine(store, r, model="fake", client=client, registry={})
    assert res["status"] == "done" and res["session"].startswith("rk_"), res
    st = store.state(r)
    assert len(st.runs) == 1 and st.runs[0]["status"] == "done" and st.runs[0]["cost"] >= 0.0
    last = (r.path / "last-run.md").read_text(encoding="utf-8")
    assert "digest written" in last and "status: done" in last
    print("routine-run: exec drives it, odometer settles, last-run.md lands  ✓")

    # --- trajectory: dream food, picker-invisible, heuristic outcome LAST ---
    traj = sorted(S.global_traj_dir().glob("*.jsonl"))[-1]
    recs = [json.loads(l) for l in traj.read_text(encoding="utf-8").splitlines()]
    meta = next(x["data"] for x in recs if x["kind"] == "meta")
    assert meta["runner"] == "routine" and meta["routine"] == "arxiv-digest"
    assert meta["project_id"], "dream needs project identity"
    outcomes = [x["data"] for x in recs if x["kind"] == "outcome"]
    assert len(outcomes) == 2 and outcomes[-1]["source"] == "heuristic", \
        "heuristic record must land LAST (judge gate keys on it)"
    assert S._read_info(traj) is None, "routine runs must stay out of the resume picker"
    print("routine-run: trajectory = dream food, picker-invisible  ✓")

    # --- grants: ungranted risky tool blocks; granting it in the toml runs it ---
    r2 = make_routine(store, "deployer", tools=[])
    turns = [[chunk(tool_calls=[tc(0, "c1", "deploy", "{}")]), chunk(usage=U())],
             [chunk(content="deployed fine."), chunk(usage=U())]]
    res2 = await run_routine(store, r2, model="fake",
                             client=make_client(turns), registry={"deploy": DEPLOY})
    assert res2["status"] == "blocked" and res2["blocked_on"]["grant"] == "tool:deploy", res2
    assert store.state(r2).runs[-1]["status"] == "blocked"

    r3 = make_routine(store, "deployer-granted", tools=["deploy"])
    res3 = await run_routine(store, r3, model="fake",
                             client=make_client(turns), registry={"deploy": DEPLOY})
    assert res3["status"] == "done", res3
    print("routine-run: the toml IS the grant envelope — block without, run with  ✓")

    print("ROUTINE-RUN SMOKE OK — scheduled trust, spent carefully. amaze!")


asyncio.run(main())
