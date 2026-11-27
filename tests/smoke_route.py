"""One route (engine/route.py): where a model's requests go, and with which key.

Each block below is a way two entry points used to disagree:

- a model on another provider needed the home key anyway (`exec`), or was
  quietly sent to the home URL when its own key was missing (the launch);
- `goal` and `/goal` always used the home key and URL, whatever the model;
- an `explore` child rode its parent's client but spoke the default wire shape;
- an auth error named the home key and URL even when neither was involved.

No network: every request lands in a recording transport.
"""
import asyncio
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import httpx
import typer
from openai import AsyncOpenAI
from typer.testing import CliRunner

home = Path(tempfile.mkdtemp(prefix="rocky-route-"))
os.environ["ROCKYCODE_HOME"] = str(home)
os.environ["ROCKYCODE_OLLAMA_URL"] = "http://127.0.0.1:9"  # nothing listens there: the local server is down
for _name in [n for n in os.environ if re.fullmatch(r"ROCKYCODE_(\w+_)?(API_KEY|BASE_URL)|ROCKYCODE_MODEL", n)]:
    os.environ.pop(_name)

from rockycode import onboarding  # noqa: E402

onboarding._keyring_key = lambda: None  # hermetic — never read the real OS keychain

from rockycode.cli import _common, app, firstrun  # noqa: E402
from rockycode.engine import providers as P  # noqa: E402
from rockycode.engine import route as R  # noqa: E402
from rockycode.engine.events import EngineError, TurnFinished  # noqa: E402
from rockycode.engine.explore import build_explore_tool  # noqa: E402
from rockycode.engine.loop import Engine  # noqa: E402
from rockycode.goal.budget import GoalBudget  # noqa: E402
from rockycode.goal.session import LiveGoalBackend  # noqa: E402

CATALOG = P.catalog()
KIMI_URL = CATALOG["kimi"].endpoints[0].base_url
MINIMAX_URL = CATALOG["minimax"].endpoints[0].base_url
PLAN_URL = next(e for e in CATALOG["qwen"].endpoints if e.eid == "qwen-plan").base_url
HOME_URL = onboarding.DEFAULT_BASE_URL
GATEWAY = "https://gateway.example/v1"


@contextmanager
def keys(**env):
    """Exactly these rocky variables, and no others."""
    with patch.dict(os.environ, env):
        yield


def refused(spec, **kw) -> R.RouteError:
    try:
        R.route(spec, **kw)
    except R.RouteError as error:
        return error
    raise AssertionError(f"{spec}: expected no route")


class Wire:
    """Records (url, authorization, body) and answers like a chat endpoint."""

    def __init__(self):
        self.requests, self.answers, self.status, self.opened = [], [], 200, []

    def client(self, **kw):
        self.opened.append(kw)  # each client owns its transport: closing one must not close the rest
        return AsyncOpenAI(http_client=httpx.AsyncClient(
            trust_env=False, transport=httpx.MockTransport(self._respond)), **kw)

    def _respond(self, request):
        body = json.loads(request.content)
        self.requests.append((str(request.url), request.headers["Authorization"], body))
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "rejected"}})
        text = self.answers.pop(0) if self.answers else "done"
        if not body.get("stream"):
            return httpx.Response(200, json={
                "id": "fixture", "object": "chat.completion", "created": 0, "model": "fixture",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})
        chunks = [{"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
                  {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
        for c in chunks:
            c.update(id="fixture", object="chat.completion.chunk", created=0, model="fixture")
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n")


# ── the rule ─────────────────────────────────────────────────────────────────
with keys(ROCKYCODE_KIMI_API_KEY="k-kimi"):
    to = R.route("kimi-k3")
    assert (to.base_url, to.key, to.key_env, to.via) == (KIMI_URL, "k-kimi", "ROCKYCODE_KIMI_API_KEY", "endpoint")
    assert to.known and to.model == "kimi-k3" and to.choice.id == "kimi:kimi-k3"
    assert "k-kimi" not in repr(to) and "k-kimi" not in to.describe(), "the key never prints"
    assert to.describe() == f"ROCKYCODE_KIMI_API_KEY → {KIMI_URL}"
print("route: a model goes to its own endpoint with that endpoint's key, home key or not  ✓")

with keys(ROCKYCODE_API_KEY="k-home"):
    error = refused("kimi-k3")
    assert error.key_env == "ROCKYCODE_KIMI_API_KEY" and "ROCKYCODE_KIMI_API_KEY" in str(error)
    assert "another provider" in str(error)
    own = R.route("deepseek-flash")
    assert (own.base_url, own.key, own.key_env, own.via) == (HOME_URL, "k-home", "ROCKYCODE_API_KEY", "endpoint")
    unlisted = R.route("my-finetune-7")
    assert (unlisted.base_url, unlisted.key, unlisted.via) == (HOME_URL, "k-home", "home")
    assert not unlisted.known and unlisted.wire_model == "my-finetune-7"
    typo = refused("kimi:k9")
    assert "'k9' is not exactly one of kimi's models" in str(typo) and typo.key_env == "", \
        "a known endpoint with an unknown model is a typo, not a job for the home endpoint"
print("route: a missing key is an error that names the variable — nothing rides the home key  ✓")

with keys(ROCKYCODE_API_KEY="k-gateway", ROCKYCODE_BASE_URL=GATEWAY):
    assert R.gateway()
    to = R.route("kimi-k3")
    assert (to.base_url, to.key, to.key_env, to.via) == (GATEWAY, "k-gateway", "ROCKYCODE_API_KEY", "gateway")
    assert to.provider.name == "kimi" and to.wire_model == "kimi-k3", "the model's own contract still applies"
    assert refused("kimi-k3", gateway_ok=False).key_env == "ROCKYCODE_KIMI_API_KEY"
    assert refused("qwen-plan:qwen4-max").key_env == "ROCKYCODE_QWEN_PLAN_API_KEY", \
        "a plan-only model is served by its plan and nothing else"
    for saved in ("qwen-plan:qwen4-max-preview", "qwen4-max-preview"):
        assert refused(saved).key_env == "ROCKYCODE_QWEN_PLAN_API_KEY", \
            "preview aliases keep the plan key requirement, including on a gateway"
    with keys(ROCKYCODE_KIMI_API_KEY="k-kimi"):
        assert R.route("kimi-k3").via == "endpoint", "its own key always wins"
with keys(ROCKYCODE_API_KEY="k-home", ROCKYCODE_BASE_URL=HOME_URL):
    assert not R.gateway(), "the default URL spelled out is not a gateway"
print("route: with ROCKYCODE_BASE_URL on your own gateway, an unkeyed model rides it; plan-only never  ✓")

with keys():
    assert refused("deepseek-flash").key_env == "ROCKYCODE_API_KEY"
    assert refused("my-finetune-7").key_env == "ROCKYCODE_API_KEY"
    down = refused("ollama:qwen3.8:27b-mlx")
    assert "isn't running" in str(down) and down.key_env == "", str(down)
print("route: nothing configured, or a local server that is down, says exactly that  ✓")


# ── the engine ───────────────────────────────────────────────────────────────
async def engine_cases():
    wire = Wire()
    with patch("openai.AsyncOpenAI", side_effect=wire.client):
        with keys(ROCKYCODE_MINIMAX_API_KEY="k-minimax"):
            engine = Engine("minimax-m3.1-flash", workdir=home, registry={}, max_steps=2)
            assert wire.opened[-1] == {"api_key": "k-minimax", "base_url": MINIMAX_URL,
                                       "max_retries": 5, "timeout": 300.0}
            assert engine.route.via == "endpoint" and engine.wire_model == "MiniMax-M3.1-Flash"
            assert engine.reasoning_policy == "minimax" and engine.provider_name == "minimax"
            meta = json.loads(engine.trajectory.path.read_text().splitlines()[0])["data"]
            assert meta["base_url"] == MINIMAX_URL, "the session records where it really went"
            assert any(isinstance(ev, TurnFinished) for ev in [ev async for ev in engine.run_turn("hi")])
            url, auth, body = wire.requests[-1]
            assert url == MINIMAX_URL + "/chat/completions" and auth == "Bearer k-minimax"
            assert body["model"] == "MiniMax-M3.1-Flash" and body["reasoning_split"] is True
            print("engine: launches on another provider with no home key at all  ✓")

            # An explore child rides the parent's client: it must speak the parent
            # model's wire id and reasoning shape, not the default ones.
            before = len(wire.requests)
            report = await build_explore_tool(engine)["explore"].fn(task="where is main()?", role="explore")
            assert not report.startswith("[error]"), report
            child = [body for _, _, body in wire.requests[before:]]
            assert child and all(b["model"] == "MiniMax-M3.1-Flash" for b in child), [b["model"] for b in child]
            assert all(b["thinking"] == {"type": "adaptive"} and b["reasoning_split"] is True for b in child)
            assert all(auth == "Bearer k-minimax" for _, auth, _ in wire.requests[before:])
            print("explore: the child takes the parent's route, so it speaks the parent model's shape  ✓")

            # Auth trouble names the key and the URL that were really used.
            wire.status = 401
            message = next(ev.message for ev in [ev async for ev in engine.run_turn("again")]
                           if isinstance(ev, EngineError))
            wire.status = 200
            assert f"key source: ROCKYCODE_MINIMAX_API_KEY → {MINIMAX_URL}" in message, message
            assert "k-minimax" not in message and "deepseek" not in message
            print("engine: an auth error names this route's key and URL, never the home pair  ✓")

        with keys(ROCKYCODE_API_KEY="k-home"):
            opened = len(wire.opened)
            try:
                Engine("minimax-m3.1-flash", workdir=home, registry={})
            except R.RouteError as error:
                assert "ROCKYCODE_MINIMAX_API_KEY" in str(error)
            else:
                raise AssertionError("an unkeyed model must not launch on the home key")
            assert len(wire.opened) == opened, "and no client was opened for it"
        with keys(ROCKYCODE_API_KEY="k-gateway", ROCKYCODE_BASE_URL=GATEWAY):
            engine = Engine("minimax-m3.1-flash", workdir=home, registry={})
            assert wire.opened[-1]["base_url"] == GATEWAY and wire.opened[-1]["api_key"] == "k-gateway"
            assert engine.route.via == "gateway" and engine.wire_model == "MiniMax-M3.1-Flash"
        print("engine: no silent home fallback; your own gateway still serves every model  ✓")

        # A host that opened the transport itself hands the route along with it.
        with keys(ROCKYCODE_MINIMAX_API_KEY="k-minimax"):
            to = R.route("minimax-m3.1-flash")
            shared = R.open_client(to)
            engine = Engine("minimax-m3.1-flash", client=shared, route=to, workdir=home, registry={})
            assert engine.client is shared and engine.wire_model == "MiniMax-M3.1-Flash"
        bare = Engine("minimax-m3.1-flash", client=shared, workdir=home, registry={})
        assert bare.route is None and bare.wire_model == "minimax-m3.1-flash", \
            "an injected client with no route is still never second-guessed"
        print("engine: client + route travel together; a bare injected client is left alone  ✓")


asyncio.run(engine_cases())


# ── goal ─────────────────────────────────────────────────────────────────────
async def goal_cases():
    wire = Wire()
    ws = NS(path=home, origin=home, branch="goal/fake", cleanup=lambda **kw: None)
    with patch("openai.AsyncOpenAI", side_effect=wire.client), \
         patch("rockycode.goal.worktree.GoalWorkspace.create", return_value=ws):
        # Work on one provider, review on another, and no home key anywhere.
        with keys(ROCKYCODE_MINIMAX_API_KEY="k-minimax", ROCKYCODE_KIMI_API_KEY="k-kimi"):
            backend = LiveGoalBackend("synthetic", model="minimax-m3.1-flash", reviewer_model="kimi-k3",
                                      budget=GoalBudget(max_usd=1), workdir=home)
            await backend.setup()
            wire.answers = ["1. Inspect code", "KEEP"]
            await backend.driver.plan("synthetic")
            await backend.driver.review("synthetic", ["Inspect code"], [], "checkpoint")
            (p_url, p_auth, planner), (r_url, r_auth, reviewer) = wire.requests[-2:]
            assert (p_url, p_auth) == (MINIMAX_URL + "/chat/completions", "Bearer k-minimax")
            assert planner["model"] == "MiniMax-M3.1-Flash" and planner["thinking"] == {"type": "adaptive"}
            assert (r_url, r_auth) == (KIMI_URL + "/chat/completions", "Bearer k-kimi")
            assert reviewer["model"] == "kimi-k3" and "thinking" not in reviewer
            assert backend._route.base_url == MINIMAX_URL
            await backend.cleanup(keep=False)
        print("goal: work and review each use their own model's endpoint, key and shape  ✓")

        # A chat session hands over its own route: the model id alone would have
        # picked the provider's first endpoint, not the plan the session is on.
        with keys(ROCKYCODE_QWEN_PLAN_API_KEY="k-plan"):
            session_route = R.route("qwen-plan:qwen3.8-max")
            backend = LiveGoalBackend("synthetic", model="qwen3.8-max", reviewer_model="qwen3.8-max",
                                      budget=GoalBudget(max_usd=1), workdir=home, route=session_route)
            await backend.setup()
            wire.answers = ["1. Inspect code"]
            await backend.driver.plan("synthetic")
            url, auth, body = wire.requests[-1]
            assert (url, auth) == (PLAN_URL + "/chat/completions", "Bearer k-plan"), (url, auth)
            assert body["enable_thinking"] is False
            await backend.cleanup(keep=False)
        print("goal: /goal keeps the session's plan or custom endpoint  ✓")


asyncio.run(goal_cases())


# ── the commands ─────────────────────────────────────────────────────────────
runner = CliRunner()
workdir = home / "project"
workdir.mkdir()
(workdir / "note.txt").write_text("pineapple\n")
wire = Wire()
with patch("openai.AsyncOpenAI", side_effect=wire.client):
    with keys(ROCKYCODE_KIMI_API_KEY="k-kimi"):
        result = runner.invoke(app, ["exec", "--model", "kimi-k3", "--profile", "read", "--no-sandbox",
                                     "--max-steps", "2", "-C", str(workdir), "say done"])
        assert result.exit_code == 0, result.output
        assert wire.requests[-1][0] == KIMI_URL + "/chat/completions" and wire.requests[-1][1] == "Bearer k-kimi"
    with keys(ROCKYCODE_API_KEY="k-home"):
        sent = len(wire.requests)
        result = runner.invoke(app, ["exec", "--model", "kimi-k3", "--profile", "read", "--no-sandbox",
                                     "-C", str(workdir), "say done"])
        assert result.exit_code == 1 and "ROCKYCODE_KIMI_API_KEY" in result.output.replace("\n", ""), result.output
        result = runner.invoke(app, ["goal", "tidy the notes", "--model", "kimi-k3", "--workdir", str(workdir)])
        assert result.exit_code == 1 and "ROCKYCODE_KIMI_API_KEY" in result.output.replace("\n", ""), result.output
        result = runner.invoke(app, ["bench", "--model", "kimi-k3", "--limit", "1"])
        assert result.exit_code == 1 and "ROCKYCODE_KIMI_API_KEY" in result.output.replace("\n", ""), result.output
        assert len(wire.requests) == sent, "nothing was sent anywhere"
print("exec: a task on another provider needs no home key; a missing key is one line, exit 1  ✓")
print("goal, bench: a model without its key stops before any workspace, container or dataset  ✓")

def stopped(model) -> bool:
    try:
        _common._launch_model(model)
    except typer.Exit as stop:
        return stop.exit_code == 1
    return False


terminal = patch.object(sys, "stdin", NS(isatty=lambda: True))
with patch.object(firstrun, "run_setup") as setup:
    with keys(ROCKYCODE_KIMI_API_KEY="k-kimi"):
        assert _common._launch_model("kimi-k3") == "kimi-k3"
        with keys(ROCKYCODE_MODEL="kimi:k3"):
            assert _common._launch_model(None) == "kimi:k3"
    assert not setup.called, "a model that has its own key never triggers first-run setup"
    with keys(ROCKYCODE_API_KEY="k-home"), terminal:
        assert stopped("kimi-k3"), "the stubbed setup added no key, so the launch still stops"
    asked_for = setup.call_args.args[1]
    assert asked_for.endpoint.eid == "kimi" and asked_for.key_env == "ROCKYCODE_KIMI_API_KEY", \
        "the flow is asked for THIS model's key, never a different provider's"
    with keys(ROCKYCODE_API_KEY="k-home"), patch.object(sys, "stdin", NS(isatty=lambda: False)):
        assert stopped("kimi-k3") and setup.call_count == 1, "no terminal to ask in: one line and stop"
    with keys(), terminal:
        assert stopped(None) and setup.call_args.args[1] is None, "nothing set up at all: the full first run"
        assert stopped("ollama:qwen3.8:27b-mlx") and setup.call_count == 2, \
            "a local server that is down is not something a key fixes"
print("launch: set up → start; one key missing → ask for that key; no terminal → say so and stop  ✓")

# ── audit: SDK clients are opened in one place ───────────────────────────────
ROOT = Path(__file__).resolve().parents[1] / "rockycode"
# route.py opens every model client. The rest: the first-run key probe (the key
# is not saved yet), the local embedding index, and two `python -m` diagnostics.
ALLOWED = {"engine/route.py", "onboarding.py", "memory/index.py", "model_probe.py", "flash_probe.py"}
found = {str(f.relative_to(ROOT)) for f in ROOT.rglob("*.py")
         if re.search(r"\b(?:Async)?OpenAI\(", f.read_text(encoding="utf-8"))}
assert found == ALLOWED, f"SDK clients opened outside engine/route.py: {sorted(found - ALLOWED)}"
print("audit: every model client is opened by engine/route.py  ✓")

print("ROUTE SMOKE OK — one model, one road, one key. amaze!")
