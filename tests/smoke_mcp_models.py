"""Native MCP model contracts over HTTPX fake transport, no paid APIs or Docker."""
import asyncio
import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from smoke_mcp_server import _with_client
from smoke_mcp_capabilities import call, rejected, wait_job

import httpx
from openai import AsyncOpenAI
from rockycode import mcp_features as features
from rockycode.engine import providers as P
from rockycode.model_selection import select_model, model_client, model_receipt, text_completion
from rockycode.goal.session import LiveGoalBackend
from rockycode.goal.budget import GoalBudget
from rockycode.engine.tools import Tool, _fn_schema
from rockycode.pricing import UsageLedger, load_pricing
from rockycode.routines import Routine, RoutineStore
from rockycode.session import get_project, global_traj_dir

ROOT = Path(tempfile.mkdtemp(prefix="rocky-model-contract-")).resolve()
PROJECT = ROOT / "project"
PROJECT.mkdir()
USAGE = {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60,
         "prompt_tokens_details": {"cached_tokens": 20}}
KEYS = {"ROCKYCODE_MINIMAX_API_KEY": "mini-test-key", "ROCKYCODE_QWEN_API_KEY": "qwen-test-key",
        "ROCKYCODE_KIMI_API_KEY": "kimi-test-key", "ROCKYCODE_GLM_API_KEY": "glm-test-key"}


class Wire:
    def __init__(self):
        self.requests = []
        self.clients = []
        self.answers = []
        self.status = 200
        self.finish = "stop"
        self.tool = None
        self.entered = None
        self.usage = USAGE

    async def handle(self, request):
        body = json.loads(request.content)
        self.requests.append((str(request.url), request.headers["authorization"], body))
        if self.entered:
            self.entered.set()
            await asyncio.Event().wait()
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "private-prompt-and-key"}})
        text = self.answers.pop(0) if self.answers else "PASS — verified"
        message = {"role": "assistant", "content": text}
        reason = self.finish
        if self.tool:
            message["tool_calls"] = [{"id": "call_1", "type": "function", "function": {
                "name": "bash", "arguments": json.dumps({"command": self.tool})}}]
            reason = "tool_calls" if self.finish == "stop" else self.finish
        response = {"id": "fake", "object": "chat.completion", "created": 1,
                    "model": body["model"], "usage": self.usage,
                    "choices": [{"index": 0, "message": message, "finish_reason": reason}]}
        if not body.get("stream"):
            return httpx.Response(200, json=response)
        if self.tool:
            message["tool_calls"][0]["index"] = 0
        chunk = {**response, "object": "chat.completion.chunk", "choices": [
            {"index": 0, "delta": message, "finish_reason": reason}]}
        data = "data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)

    def client(self, **kwargs):
        kwargs["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        client = AsyncOpenAI(**kwargs)
        self.clients.append(client)
        return client


def raises(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except (ValueError, RuntimeError):
        return
    raise AssertionError("expected explicit refusal")


async def text_contracts(wire):
    # All catalog models use their OWN wire ID/effort shape, not a common DeepSeek body.
    for p in P.catalog().values():
        for mid in p.models:
            c = select_model(f"{p.endpoints_for(mid)[0].eid}:{mid}")
            with patch.dict(os.environ, {c.endpoint.key_env: "model-specific-key"}):
                async with model_client(c) as client:
                    await text_completion(client, c, [{"role": "user", "content": "synthetic"}], 2048)
            url, auth, body = wire.requests[-1]
            assert url == c.endpoint.base_url.rstrip("/") + "/chat/completions"
            assert auth == "Bearer model-specific-key" and body["model"] == p.api_model(mid)
            # The output cap goes in the field the model's row names, and only there.
            assert {k: v for k, v in body.items() if k.startswith("max_")} == {
                c.spec.protocol.output_limit: min(2048, c.spec.max_output)}
            if p.reasoning == "enable_thinking":
                assert body["enable_thinking"] is False and "thinking" not in body
            if p.reasoning == "effort":
                assert "thinking" not in body and body["reasoning_effort"] == "low"
            if mid == "minimax-m3.1-flash":
                assert body["model"] == "MiniMax-M3.1-Flash"
                assert body["thinking"] == {"type": "adaptive"}
                assert body["reasoning_split"] and body["reasoning_effort"] == "low"
            if mid == "deepseek-flash":
                assert body["temperature"] == 1 and "top_p" not in body

    # Live-discovered local models are keyless, explicitly free, and receive no
    # cloud reasoning flags. No network probe is made by this fixture.
    with patch.object(P, "local_models", return_value=(["qwen3.5:4b"], True)):
        from rockycode.model_selection import require_usd_price
        local = select_model("ollama:qwen3.5:4b", tools=True)
        ledger = UsageLedger()
        require_usd_price(local, ledger)
        async with model_client(local) as client:
            await text_completion(client, local, [{"role": "user", "content": "synthetic"}], 2048,
                                  ledger=ledger)
        assert ledger.cost("usd") == 0 and ledger.priced(local.model)
        body = wire.requests[-1][2]
        assert body["model"] == "qwen3.5:4b"
        assert not {"thinking", "reasoning_effort", "enable_thinking"} & body.keys()

    for bad in ("", "missing-model", "https://attacker.invalid/v1", "qwen:missing"):
        raises(select_model, bad)
    c = select_model("minimax:minimax-m3.1-flash")
    without_mini = {k: v for k, v in os.environ.items() if not k.startswith("ROCKYCODE_MINIMAX")}
    with patch.dict(os.environ, without_mini, clear=True):
        raises(model_client, c)  # DeepSeek key exists, but MUST NOT be used
    cp = copy.deepcopy(c)
    cp.provider.tools = "off"
    with patch.object(P, "resolve", return_value=(cp.provider, cp.endpoint, cp.model)):
        raises(select_model, cp.id, tools=True)

    async with model_client(c) as client:
        for status, reason, tool in ((401, "stop", None), (429, "stop", None),
                                     (200, "length", None), (200, None, None), (200, "stop", "echo no")):
            wire.status, wire.finish, wire.tool = status, reason, tool
            before = len(wire.requests)
            try:
                await text_completion(client, c, [{"role": "user", "content": "synthetic"}], 2048)
            except RuntimeError as exc:
                assert "private-prompt-and-key" not in str(exc)
            else:
                raise AssertionError("error/incomplete/tool response accepted")
            assert len(wire.requests) == before + 1  # no retry or provider fallback
    wire.status, wire.finish, wire.tool = 200, "stop", None


async def mcp_checks(session, wire):
    await session.initialize()
    schemas = {t.name: t.inputSchema for t in (await session.list_tools()).tools}
    for name in ("rocky_dream", "rocky_goal", "rocky_routine_run"):
        assert "model" in schemas[name]["properties"]
        assert not {"base_url", "api_key", "key_env"} & schemas[name]["properties"].keys()

    mini = "minimax:minimax-m3.1-flash"
    qwen = next(p.models[0] for p in P.catalog().values() if p.name == "qwen")
    goal = dict(objective="Inspect code", plan="1. Inspect code", workdir=str(PROJECT),
                model=mini, reviewer_model=f"qwen:{qwen}")
    before = len(wire.requests)
    preview = await call(session, "rocky_goal", **goal)
    assert len(wire.requests) == before and not (PROJECT / ".rockycode").exists()
    assert preview["models"]["reviewer_verifier"]["provider"] == "qwen"
    for changes in ({"model": f"qwen:{qwen}"}, {"reviewer_model": mini}, {"network": True},
                    {"max_usd": 2}, {"max_tokens": 42}):
        await rejected(session, "rocky_goal", **{**goal, **changes}, run=True,
                       plan_digest=preview["plan_digest"])
    await rejected(session, "rocky_goal", **goal, run=True, plan_digest=preview["plan_digest"],
                   grants=["push"])
    await rejected(session, "rocky_goal", **{**goal, "plan": "rm -rf /"})
    without_qwen = {k: v for k, v in os.environ.items() if not k.startswith("ROCKYCODE_QWEN")}
    with patch.dict(os.environ, without_qwen, clear=True):
        await rejected(session, "rocky_goal", **goal, run=True, plan_digest=preview["plan_digest"])
    assert len(wire.requests) == before


    # Dream has no implicit disclosure, even dry_run; warm reflections and unknown
    # historical model provenance cannot evade the explicit project-context grant.
    pid = get_project(PROJECT).id
    traj = global_traj_dir()
    traj.mkdir(parents=True, exist_ok=True)
    for sid, recorded, warm in (("old-cold", "deepseek-flash", False), ("old-warm", "", True)):
        records = [{"kind": "meta", "data": {"project_id": pid, "model": recorded}}]
        records += [{"kind": "message", "data": {"role": role, "content": "private-transcript-marker"}}
                    for role in ("system", "user", "assistant")]
        if warm:
            records.append({"kind": "reflect", "data": {"score": 0.8,
                "episode": {"task": "warm-private-marker", "outcome": "success"}, "facts": []}})
        (traj / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in records))
    dream = dict(workdir=str(PROJECT), model=mini, limit=2)
    before = len(wire.requests)
    dp = await call(session, "rocky_dream", **dream)
    assert len(wire.requests) == before
    await rejected(session, "rocky_dream", **dream, run=True, digest=dp["digest"])
    await rejected(session, "rocky_dream", **{**dream, "model": f"qwen:{qwen}"},
                   run=True, digest=dp["digest"], share_project_context=True)
    assert len(wire.requests) == before
    # Cold reflect parses a valid result; warm reflection also reaches selected provider.
    wire.answers = [json.dumps({"grade": {"completion": 1, "adherence": 1, "efficiency": 1,
                       "user_feeling": 1, "rationale": "synthetic"},
                    "episode": {"task": "cold", "outcome": "success", "worked": [], "failed": []},
                    "facts": [], "patterns": [], "skills_used": [], "summary": "synthetic"}),
                    "null", "- synthetic state"]
    job = await call(session, "rocky_dream", **dream, run=True, digest=dp["digest"], share_project_context=True)
    result = await wait_job(session, job["id"])
    assert result["status"] == "finished", result
    sent = wire.requests[before:]
    assert any("private-transcript-marker" in json.dumps(b) for _, _, b in sent)
    assert any("warm-private-marker" in json.dumps(b) for _, _, b in sent)
    assert all(b["model"] == "MiniMax-M3.1-Flash" for _, _, b in sent)
    assert not result["result"]["routine_tests_executed"]
    wire.answers.clear()
    wire.status = 401
    before = len(wire.requests)
    job = await call(session, "rocky_dream", **dream, run=True, digest=dp["digest"], share_project_context=True)
    result = await wait_job(session, job["id"])
    assert result["status"] == "error" and "private-prompt-and-key" not in json.dumps(result)
    assert len(wire.requests) == before + 1
    wire.status = 200

    # Real routine -> run_exec -> Engine with fake sandbox/tools. Asking for push
    # does not inherit authorization from a model choice or a preview digest.
    rstore = RoutineStore()
    routine = Routine(name="inspect", prompt="Inspect code", workdir=str(PROJECT), project_id=pid, max_steps=2)
    rstore.save(routine)
    args = dict(name=routine.name, workdir=str(PROJECT), model=mini, max_tokens=2048)
    rp = await call(session, "rocky_routine_run", **args)
    assert rp["finalize_steps"] == 3 and "not an enforced" in rp["budget_note"]
    for changes in ({"max_tokens": 4096}, {"timeout_s": 901}, {"model": f"qwen:{qwen}"}):
        await rejected(session, "rocky_routine_run", **{**args, **changes}, run=True, digest=rp["digest"])
    effects, stopped = [], []
    async def bash(command):
        effects.append(command)
        return "synthetic"
    registry = {"bash": Tool("bash", _fn_schema("bash", "fake", {
        "command": {"type": "string"}}, ["command"]), bash, "risky")}
    async def stop():
        stopped.append(True)
    async def sandbox_start(workdir, *, network):
        assert not network
        return SimpleNamespace(container_id="fake-only", stop=stop)
    wire.tool = "git push origin main"
    with patch("rockycode.engine.sandbox.ChatSandbox.start", sandbox_start), \
         patch("rockycode.engine.sandbox.build_sandbox_registry", return_value=registry):
        job = await call(session, "rocky_routine_run", **args, run=True, digest=rp["digest"])
        result = await wait_job(session, job["id"])
        assert result["result"]["status"] == "blocked" and not effects, result
        assert result["result"]["blocked_on"]
        assert wire.requests[-1][2]["max_tokens"] == 2048
        assert wire.requests[-1][1] == "Bearer mini-test-key" and stopped
        # Runaway safe calls hit the existing finite step/finalization budget.
        wire.tool = "echo synthetic"
        job = await call(session, "rocky_routine_run", **args, run=True, digest=rp["digest"])
        result = await wait_job(session, job["id"])
        assert result["result"]["status"] == "budget", result
        assert len(effects) <= routine.max_steps + 3
        # A truncated native reply must not execute its proposed tool batch.
        wire.finish = "length"
        effect_count = len(effects)
        job = await call(session, "rocky_routine_run", **args, run=True, digest=rp["digest"])
        result = await wait_job(session, job["id"])
        assert result["result"]["status"] == "error" and len(effects) == effect_count
        wire.finish = "stop"
        # Cancellation closes client and stops the sandbox before returning.
        wire.tool = None
        wire.entered = asyncio.Event()
        job = await call(session, "rocky_routine_run", **args, run=True, digest=rp["digest"])
        await wire.entered.wait()
        cancelled = await call(session, "rocky_job_cancel", job_id=job["id"])
        assert cancelled["status"] == "cancelled" and wire.clients[-1].is_closed()
        wire.entered = None
    assert not rstore.load(routine.name).auto and rstore.load(routine.name).lease_deadline == 0
    # Execute the real MCP Goal backend with an isolated workspace stand-in.
    # There is no planner request for the reviewed host plan; worker/verifier
    # use their own native transports. A failed worker cannot become "done".
    cleanup = []
    ws = SimpleNamespace(path=PROJECT, origin=PROJECT, branch="goal/fake", base="fake-base",
                         cleanup=lambda **kw: cleanup.append(kw), commit=lambda msg: True,
                         diff=lambda: "synthetic diff")
    with patch("rockycode.goal.worktree.GoalWorkspace.create", return_value=ws), \
         patch("rockycode.engine.sandbox.ChatSandbox.start", sandbox_start), \
         patch("rockycode.engine.sandbox.build_sandbox_registry", return_value=dict(registry)), \
         patch("rockycode.engine.checks.run_checks", return_value="clean"):
        before = len(wire.requests)
        job = await call(session, "rocky_goal", **goal, run=True, plan_digest=preview["plan_digest"])
        result = await wait_job(session, job["id"])
        assert result["result"]["status"] == "done", result
        assert len(wire.requests) == before + 2  # worker + grounded verify only
        assert wire.requests[-2][1] == "Bearer mini-test-key"
        assert wire.requests[-1][1] == "Bearer qwen-test-key"
        assert cleanup[-1] == {"keep": True}
        for status, usage in ((401, USAGE), (200, None)):
            wire.status, wire.usage = status, usage
            before = len(wire.requests)
            job = await call(session, "rocky_goal", **goal, run=True, plan_digest=preview["plan_digest"])
            result = await wait_job(session, job["id"])
            assert result["result"]["status"] == "error", result
            assert "private-prompt-and-key" not in json.dumps(result)
            assert len(wire.requests) == before + 1
        wire.status, wire.usage = 200, USAGE
        wire.entered = asyncio.Event()
        job = await call(session, "rocky_goal", **goal, run=True, plan_digest=preview["plan_digest"])
        await wire.entered.wait()
        cancelled = await call(session, "rocky_job_cancel", job_id=job["id"])
        assert cancelled["status"] == "cancelled"
        assert all(c.is_closed() for c in wire.clients)
        wire.entered = None


    # Endpoint/model config changes while waiting never send keys to the new URL.
    await features.jobs.lane.acquire()
    try:
        job = await call(session, "rocky_routine_run", **args, run=True, digest=rp["digest"])
        before = len(wire.requests)
        changed = copy.deepcopy(select_model(mini))
        changed.endpoint.base_url = "https://changed.invalid/v1"
        with patch.object(P, "resolve", return_value=(changed.provider, changed.endpoint, changed.model)):
            features.jobs.lane.release()
            result = await wait_job(session, job["id"])
            assert result["status"] == "error" and "changed" in result["error"]
            assert len(wire.requests) == before
    finally:
        if features.jobs.lane.locked():
            features.jobs.lane.release()


async def goal_contracts(wire):
    mini = select_model("minimax:minimax-m3.1-flash")
    qwen = select_model("qwen")
    ws = SimpleNamespace(path=PROJECT, origin=PROJECT, branch="goal/fake", cleanup=lambda **kw: None)
    backend = LiveGoalBackend("synthetic", model=mini.model, reviewer_model=qwen.model,
                              model_choice=mini, reviewer_choice=qwen, budget=GoalBudget(max_usd=1),
                              workdir=PROJECT)
    with patch("rockycode.goal.worktree.GoalWorkspace.create", return_value=ws):
        await backend.setup()
    try:
        wire.answers = ["1. Inspect code", "KEEP"]
        await backend.driver.plan("synthetic")
        await backend.driver.review("synthetic", ["Inspect code"], [], "checkpoint")
        planner, reviewer = wire.requests[-2:]
        assert planner[1] == "Bearer mini-test-key" and planner[2]["model"] == "MiniMax-M3.1-Flash"
        assert reviewer[1] == "Bearer qwen-test-key" and reviewer[2]["model"] == qwen.provider.api_model(qwen.model)
        assert reviewer[2]["enable_thinking"] is False and "thinking" not in reviewer[2]
        # The grounded verifier's injected Engine also adopts reviewer's identity.
        with patch("rockycode.engine.checks.run_checks", return_value="clean"):
            ok, _ = await backend.driver.verify("inspect", "done")
        assert ok
        _, auth, body = wire.requests[-1]
        assert auth == "Bearer qwen-test-key" and body["stream"] and "thinking" not in body
        assert {m for m, _ in backend.ledger.buckets} == {mini.model, qwen.model}
        # Same canonical model, different endpoints must remain distinguishable.
        alternate = copy.deepcopy(mini)
        alternate.endpoint = P.Endpoint("minimax-plan", "https://plan.invalid/v1", "PLAN_TEST_KEY")
        with patch.dict(os.environ, {"PLAN_TEST_KEY": "plan-key"}):
            async with model_client(alternate) as client:
                backend.driver.reviewer_choice = alternate
                backend.driver.reviewer_client = client
                await backend.driver.review("synthetic", [], [], "checkpoint")
        assert wire.requests[-1][0].startswith("https://plan.invalid/")
        assert wire.requests[-1][1] == "Bearer plan-key"
        assert model_receipt(alternate) != model_receipt(mini)
    finally:
        await backend.cleanup(keep=False)
        await backend._client.close()
    assert all(c.is_closed() for c in wire.clients)


async def main():
    wire = Wire()
    # Qwen currently has no configured USD prices. Refuse USD accounting until the user
    # supplies prices; the cross-vendor test below supplies SYNTHETIC USD rates.
    from rockycode.model_selection import require_usd_price
    with patch.object(P, "local_models", return_value=([], False)):
        raises(require_usd_price, select_model("qwen"), UsageLedger())
    pricing = load_pricing()
    for provider in P.catalog().values():
        for mid in provider.models:
            pricing["models"].setdefault(mid, {}).setdefault(
                "usd", {"in_hit": 0.1, "in_miss": 1.0, "out": 2.0})
    # Prevent registry discovery from probing any live local server, too.
    with patch.object(P, "local_models", return_value=([], False)), \
         patch.dict(os.environ, KEYS), patch("openai.AsyncOpenAI", wire.client), \
         patch("rockycode.pricing.load_pricing", return_value=pricing):
        with patch.object(Path, "home", return_value=ROOT / "user-home"):
            await text_contracts(wire)
            await _with_client(lambda session: mcp_checks(session, wire))
            await goal_contracts(wire)
    await features.jobs.close()
    print("MCP MODEL CONTRACTS OK — native wire, consent, identities, limits, grants, cleanup")


if __name__ == "__main__":
    asyncio.run(main())
