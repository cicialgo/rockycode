"""MCP capability bridge: real protocol/store/exec, fake model and local HTTP ASGI.

No paid APIs or Docker. Tests exercise behavior at the client boundary, including
history continuation without replay, cancellation, and reviewed-content binding.
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from smoke_mcp_server import _with_client, SECRET

from rockycode import mcp_features as features
from rockycode import mcp_server as server
from rockycode import headless
from rockycode.memory import MemoryStore
from rockycode.session import get_project

ROOT = Path(tempfile.mkdtemp(prefix="rocky-mcp-caps-")).resolve()
PROJECT = ROOT / "project"
PROJECT.mkdir()
OTHER = ROOT / "other"
OTHER.mkdir()


async def call(connection, tool_name, **kwargs):
    result = await connection.call_tool(tool_name, kwargs)
    assert not result.isError, result
    body = result.structuredContent
    assert isinstance(body, dict), result
    assert SECRET not in json.dumps(body)
    return body


async def rejected(connection, tool_name, **kwargs):
    result = await connection.call_tool(tool_name, kwargs)
    assert result.isError, result
    assert SECRET not in str(result)


async def wait_job(session, job_id):
    # Synchronization yields, no timing-dependent success assertions.
    await server.jobs.items[job_id]["task"]
    return await call(session, "rocky_job_status", job_id=job_id)


async def protocol_checks(session):
    await session.initialize()
    tools = {t.name: t for t in (await session.list_tools()).tools}
    assert len(tools) == 18, sorted(tools)
    assert tools["rocky_memory"].annotations.readOnlyHint
    assert not tools["rocky_memory_write"].annotations.readOnlyHint
    caps = await call(session, "rocky_capabilities")
    assert "routine_scheduler" in caps["not_exposed"]
    models = await call(session, "rocky_models")
    assert any(m["id"] == "minimax-m3.1-flash" for m in models["models"])
    # Read discovery doesn't create project identity or require an API key.
    assert (await call(session, "rocky_sessions", workdir=str(PROJECT)))["sessions"] == []
    assert not (PROJECT / ".rockycode").exists()
    # A store beneath a symlinked ancestor is the user's layout (macOS tempdirs
    # live under /var → /private/var; dotfiles symlink ~/.rockycode), not a
    # redirect. A symlink at the store itself still is.
    real_home = ROOT / "real-home"
    real_home.mkdir()
    link_home = ROOT / "link-home"
    link_home.symlink_to(real_home, target_is_directory=True)
    with patch.dict(os.environ, {"ROCKYCODE_HOME": str(link_home / ".rockycode")}):
        assert (await call(session, "rocky_proposals", workdir=str(PROJECT)))["proposals"] == []
        assert (await call(session, "rocky_routines", workdir=str(PROJECT)))["routines"] == []
        (real_home / ".rockycode").mkdir()
        (real_home / ".rockycode" / "proposals").symlink_to(OTHER, target_is_directory=True)
        await rejected(session, "rocky_proposals", workdir=str(PROJECT))

    await call(session, "rocky_memory_write", workdir=str(PROJECT), action="save", name="boundary-rule",
               body=f"Check left boundary. {SECRET}", description="Binary search")
    found = await call(session, "rocky_memory", workdir=str(PROJECT), query="boundary")
    assert found["total"] == 1
    body = await call(session, "rocky_memory", workdir=str(PROJECT), name="boundary-rule")
    assert "[redacted]" in body["items"][0]["text"]
    await call(session, "rocky_memory_write", workdir=str(PROJECT), action="save", name="boundary-rule",
               body="Check both boundaries.")
    assert len(MemoryStore.for_workdir(PROJECT).load_all()) == 1
    await rejected(session, "rocky_memory_write", action="save", name="../escape", body="no",
                   workdir=str(PROJECT))
    await rejected(session, "rocky_memory_write", action="save", name="bad", body="no",
                   description="oops\nstatus: archived", workdir=str(PROJECT))
    (OTHER / ".rockycode").symlink_to(PROJECT / ".rockycode", target_is_directory=True)
    await rejected(session, "rocky_memory", workdir=str(OTHER))

    skill = PROJECT / ".rockycode" / "skills" / "review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: review\ndescription: Review boundaries\n---\nUse a counterexample.")
    skills = await call(session, "rocky_skills", workdir=str(PROJECT), name="review")
    assert "counterexample" in skills["items"][0]["text"]
    captured = []
    async def fake_exec(**kw):
        captured.append(kw)
        kw["write"]({"type": "result", "status": "done", "summary": "x" * 30_000, "usage": {}})
        return 0
    with patch("rockycode.headless.run_exec", fake_exec):
        result = await call(session, "rocky_exec", task="review", workdir=str(PROJECT),
                            skills=["review"], memory_names=["boundary-rule"], max_tokens=2048, effort="low")
        assert result["summary_truncated"] and len(result["summary"]) == 24_000
        assert len(Path(result["output_path"]).read_text().strip()) == 30_000
        Path(result["output_path"]).unlink()
        assert "counterexample" in captured[0]["prompt"] and "both boundaries" in captured[0]["prompt"]
        assert captured[0]["max_tokens"] == 2048 and captured[0]["reasoning_effort"] == "low"
        assert captured[0]["sandbox"] and not captured[0]["network"]
        result = await call(session, "rocky_exec", task="oops", grants=["delete"])
        assert result["exit_code"] == 1 and len(captured) == 1
        result = await call(session, "rocky_exec", task="oops", skills=["missing"], workdir=str(PROJECT))
        assert result["exit_code"] == 1 and len(captured) == 1

    # Goal execution binds approval to objective, plan, directory, budgets, and network.
    args = dict(objective="Fix boundary", plan="1. Inspect binary search\n2. Add boundary checks",
                workdir=str(PROJECT))
    preview = await call(session, "rocky_goal", **args)
    assert not (PROJECT / ".git").exists()
    for change in ({"objective": "Delete things"}, {"network": True}, {"max_usd": 2.0}, {"workdir": str(ROOT)}):
        await rejected(session, "rocky_goal", **{**args, **change}, run=True, plan_digest=preview["plan_digest"])
    await rejected(session, "rocky_goal", **args, run=True, plan_digest=preview["plan_digest"], grants=["delete"])

    # Execute the reviewed Goal through the existing backend seam, preserving cleanup.
    from rockycode.goal.session import GoalSummary, Permits
    goal_calls = []
    class Backend:
        def __init__(self, objective, **kw):
            goal_calls.append((objective, kw))
            self._client = None
        async def setup(self):
            goal_calls.append("setup")
        async def plan(self):
            return ["Inspect", "Fix"], ""
        def permits(self, plan, requires):
            return Permits(use_network=False, net_reason="", asks=[], approved=set(), blocked=None)
        async def run(self, plan, permits, emit):
            emit("milestone done")
            return GoalSummary(status="done", reason="verified", branch="goal/test")
        async def cleanup(self, keep):
            goal_calls.append(("cleanup", keep))
    with patch("rockycode.goal.session.LiveGoalBackend", Backend):
        job = await call(session, "rocky_goal", **args, run=True, plan_digest=preview["plan_digest"])
        done = await wait_job(session, job["id"])
        assert done["result"]["branch"] == "goal/test" and goal_calls[-1] == ("cleanup", True)
        assert goal_calls[0][1]["budget"].currency == "usd" and not goal_calls[0][1]["network"]

    # Real proposal installation with stale-content refusal and project scoping.
    from rockycode.dream.proposals import ProposalStore, Proposal, skills_home
    pid = get_project(PROJECT).id
    store = ProposalStore()
    p = Proposal(name="boundary-review", description="Review", project_id=pid, body="Check endpoints.")
    store.save(p)
    listed = await call(session, "rocky_proposals", workdir=str(PROJECT), name=p.name)
    digest = listed["proposals"][0]["digest"]
    p.body = "Check endpoints and empty input."
    store.save(p)
    await rejected(session, "rocky_proposal_review", workdir=str(PROJECT), name=p.name,
                   digest=digest, action="approve")
    listed = await call(session, "rocky_proposals", workdir=str(PROJECT), name=p.name)
    await call(session, "rocky_proposal_review", workdir=str(PROJECT), name=p.name,
               digest=listed["proposals"][0]["digest"], action="approve")
    assert (skills_home() / p.name / "SKILL.md").exists()

    # A hand-edited proposal cannot smuggle an automatic lease through approval.
    from rockycode.routines import Routine, RoutineStore, _emit_toml
    unsafe = Routine(name="no-auto", workdir=str(PROJECT), project_id=pid, auto=True,
                     lease_deadline=9_999_999_999)
    proposal = Proposal(name="no-auto", kind="routine", project_id=pid, body=_emit_toml(unsafe))
    store.save(proposal)
    shown = await call(session, "rocky_proposals", workdir=str(PROJECT), name="no-auto")
    await rejected(session, "rocky_proposal_review", workdir=str(PROJECT), name="no-auto",
                   digest=shown["proposals"][0]["digest"], action="approve")
    assert RoutineStore().load("no-auto") is None
    unsafe.auto, unsafe.lease_deadline = False, 0
    proposal.body = _emit_toml(unsafe)
    store.save(proposal)
    shown = await call(session, "rocky_proposals", workdir=str(PROJECT), name="no-auto")
    await call(session, "rocky_proposal_review", workdir=str(PROJECT), name="no-auto",
               digest=shown["proposals"][0]["digest"], action="approve")
    assert not RoutineStore().load("no-auto").auto

    # Decision state is scrubbed before egress and recommendations never become grants.
    from rockycode.engine.verifier import DecisionResult
    seen = []
    async def choose(self, state, question):
        seen.append((self.config, state, question))
        return DecisionResult(choice="yes", score_kind="local_option_mass", score=0.9)
    with patch("rockycode.engine.verifier.DecisionClient.choose", choose):
        result = await call(session, "rocky_decide", state=f"contains {SECRET}", question="Select?",
                            options={"yes": "Yes", "no": "No"})
    assert result["advisory_only"] and seen[0][0].provider == "ollama"
    assert SECRET not in seen[0][1]

    # Dream uses the existing runner, but cannot launch routine smoke executions.
    from rockycode.dream.core import DreamReport
    async def dream(self, limit):
        assert self.test_routines is False and self.dry_run is False and limit == 2
        return DreamReport(facts_added=1)
    with patch("rockycode.dream.DreamRunner.run", dream):
        preview = await call(session, "rocky_dream", workdir=str(PROJECT), limit=2, dry_run=False)
        job = await call(session, "rocky_dream", workdir=str(PROJECT), limit=2, dry_run=False,
                         run=True, digest=preview["digest"], share_project_context=True)
        done = await wait_job(session, job["id"])
        assert done["status"] == "finished", done
        assert done["result"]["facts_added"] == 1 and not done["result"]["routine_tests_executed"]

    # A one-shot routine needs the reviewed snapshot; no lease/scheduler is created.
    from rockycode.routines import Routine, RoutineStore
    rstore = RoutineStore()
    routine = Routine(name="review", prompt="Review only", workdir=str(PROJECT), project_id=pid)
    rstore.save(routine, skill_md="Read the changelog.")
    preview = await call(session, "rocky_routine_run", name="review", workdir=str(PROJECT))
    await rejected(session, "rocky_routine_run", name="review", workdir=str(PROJECT), run=True, digest="stale")
    async def one_shot(store, r, **kwargs):
        assert r.name == "review" and not r.auto and r.lease_deadline == 0
        return {"status": "done", "summary": "reviewed"}
    with patch("rockycode.routines.run_routine", one_shot):
        job = await call(session, "rocky_routine_run", name="review", workdir=str(PROJECT), run=True,
                         digest=preview["digest"])
        assert (await wait_job(session, job["id"]))["result"]["summary"] == "reviewed"

    # Static blocks precede any decision backend and never execute a tool.
    with patch("rockycode.engine.verifier.DecisionClient.choose", side_effect=AssertionError("must not consult")):
        review = await call(session, "rocky_review_tool", tool="bash", args={"command": "rm -rf /"},
                            task="inspect the project", workdir=str(PROJECT))
        assert review["effective"] == "block" and review["advisory_only"] and not review["consulted"]

    # Background cancellation waits for coroutine cleanup, doesn't run queued work.
    entered, release = asyncio.Event(), asyncio.Event()
    closed = []
    async def slow(**kw):
        entered.set()
        try:
            await release.wait()
        finally:
            closed.append(True)
    with patch("rockycode.headless.run_exec", slow):
        first = await call(session, "rocky_submit", task="slow", workdir=str(PROJECT))
        await entered.wait()
        second = await call(session, "rocky_submit", task="queued", workdir=str(PROJECT))
        assert (await call(session, "rocky_job_status", job_id=second["id"]))["status"] == "queued"
        await call(session, "rocky_job_cancel", job_id=second["id"])
        assert closed == []
        assert (await call(session, "rocky_job_cancel", job_id=first["id"]))["status"] == "cancelled"
        assert closed == [True]


def chunk(content=None, calls=()):
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(
        content=content, reasoning_content=None,
        tool_calls=[SimpleNamespace(index=i, id=cid, function=SimpleNamespace(name=name, arguments=json.dumps(args)))
                    for i, (cid, name, args) in enumerate(calls)]))])


class FakeClient:
    def __init__(self, turns):
        self.turns, self.requests = list(turns), []
        self.chat = SimpleNamespace(completions=self)
    async def create(self, **kw):
        self.requests.append(kw)
        assert self.turns, "unexpected model request (replay?)"
        chunks = self.turns.pop(0)
        async def stream():
            from invariants import completed_chunks
            for c in completed_chunks(chunks):
                yield c
        return stream()


async def real_exec_resume(session):
    await session.initialize()
    from rockycode.engine.tools import Tool
    count = []
    async def write_file(path, content):
        count.append(path)
        return "written once"
    async def bash(command):
        count.append(command)
        return "granted command recorded (not executed)"
    def tool(name, fn, risk):
        return Tool(name=name, fn=fn, risk=risk, schema={"type": "function", "function": {
            "name": name, "parameters": {"type": "object", "properties": {}}}})
    registry = {"write_file": tool("write_file", write_file, "moderate"), "bash": tool("bash", bash, "risky")}
    clients = [FakeClient([[chunk(calls=[("w", "write_file", {"path": "counter", "content": "1"})])],
                           [chunk(calls=[("p", "bash", {"command": "git push origin main"})])]]),
               FakeClient([[chunk(content="The prior write already happened. Continuing only with the follow-up.")]])]
    final_client = clients[-1]
    original = headless.run_exec
    async def inject(**kw):
        return await original(**kw, client=clients.pop(0), registry=registry)
    with patch("rockycode.headless.run_exec", inject):
        first = await call(session, "rocky_exec", task="write once then push", profile="full", workdir=str(PROJECT))
        assert first["exit_code"] == 2 and count == ["counter"], first
        second = await call(session, "rocky_exec", task="Report existing effects; do not retry the write",
                            profile="full", resume=first["session"], grants=[first["blocked_on"]["grant"]],
                            max_tokens=2048, effort="off")
        assert second["exit_code"] == 0 and count == ["counter"]
        assert final_client.requests[0]["max_tokens"] == 2048
        assert final_client.requests[0]["extra_body"]["thinking"]["type"] == "disabled"
        assert second["resumed_from"] == first["session"]
        Path(second["output_path"]).unlink()
        history = await call(session, "rocky_sessions", session=second["session"], workdir=str(PROJECT))
        assert any("written once" in m["text"] for m in history["messages"])
        await rejected(session, "rocky_sessions", session=second["session"], workdir=str(ROOT))


async def http_checks():
    import httpx
    token = "random-test-token-" + "a" * 32
    guarded = server.http_app(token)
    app = guarded.app
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=guarded),
                                     base_url="http://127.0.0.1:8765") as client:
            assert (await client.post("/mcp", json={})).status_code == 401
            assert (await client.post("/mcp", headers={"Authorization": "Bearer wrong"}, json={})).status_code == 401
            headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}
            payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "http-test", "version": "0"}}}
            response = await client.post("/mcp", json=payload, headers=headers)
            assert response.status_code == 200 and '"name":"rocky"' in response.text, response.text
            assert token not in response.text
            # Submit over one stateless HTTP request, poll/cancel over subsequent requests.
            entered = asyncio.Event()
            closed = []
            async def slow(**kw):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    closed.append(True)
            def body(response):
                text = next(line[6:] for line in response.text.splitlines() if line.startswith("data: "))
                return json.loads(text)["result"]["structuredContent"]
            async def http_call(name, arguments):
                response = await client.post("/mcp", headers=headers, json={
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                        "name": name, "arguments": arguments}})
                assert response.status_code == 200, response.text
                return body(response)
            with patch("rockycode.headless.run_exec", slow):
                job = await http_call("rocky_submit", {"task": "slow", "workdir": str(PROJECT)})
                await entered.wait()
                status = await http_call("rocky_job_status", {"job_id": job["id"]})
                assert status["status"] == "running" and closed == []
                await http_call("rocky_job_cancel", {"job_id": job["id"]})
                assert closed == [True]
            response = await client.post("/mcp", json=payload, headers={**headers, "Host": "evil.example"})
            assert response.status_code == 421, response.text
    with patch.dict(os.environ, {"ROCKYCODE_MCP_TOKEN": "short"}), patch("uvicorn.run") as serve:
        try:
            server.serve_http()
        except ValueError:
            pass
        else:
            raise AssertionError("weak token accepted")
        serve.assert_not_called()


async def main():
    # One loop for the shared job lock, just like the server process.
    await _with_client(protocol_checks)
    await _with_client(real_exec_resume)
    await http_checks()
    await features.jobs.close()


if __name__ == "__main__":
    asyncio.run(main())
    print("MCP CAPABILITIES OK — protocol, stores, review binding, no replay, cancellation, HTTP auth")
