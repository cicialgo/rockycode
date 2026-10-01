"""Local models (ollama): keyless endpoints, live /v1/models discovery, the
/model readiness preflight, $0 pricing, and the local compaction shape.

A stub HTTP server plays Ollama (OpenAI /v1/models + native /api/show,
/api/ps, /api/version), so every check runs without a real runtime — and a
dead port plays "not running". No network beyond loopback.
"""
import asyncio
import json
import os
import tempfile
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ["ROCKYCODE_HOME"] = tempfile.mkdtemp(prefix="rockylocal-home-")
os.chdir(tempfile.mkdtemp(prefix="rockylocal-"))

# ── stub ollama ──────────────────────────────────────────────────────────────
STATE = {
    "models": ["qwen3.8:27b-mlx", "llama3.2:3b"],
    "capabilities": ["completion", "tools"],
    "parameters": "num_ctx 65536\ntemperature 0.7",
    "model_info": {"qwen3.context_length": 262144},
    "ps": [],
}


class Stub(BaseHTTPRequestHandler):
    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            self._send({"data": [{"id": m} for m in STATE["models"]]})
        elif self.path == "/api/version":
            self._send({"version": "0.34.0"})
        elif self.path == "/api/ps":
            self._send({"models": STATE["ps"]})
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/api/show":
            self._send({"capabilities": STATE["capabilities"],
                        "parameters": STATE["parameters"],
                        "model_info": STATE["model_info"]})
        else:
            self.send_response(404); self.end_headers()

    def log_message(self, *a):  # quiet
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
ROOT = f"http://127.0.0.1:{srv.server_address[1]}"
DEAD = "http://127.0.0.1:9"  # discard port — refused instantly
os.environ["ROCKYCODE_OLLAMA_URL"] = ROOT

from rockycode import onboarding
from rockycode.engine import providers as P
from rockycode.engine.localcheck import CTX_RECOMMENDED, preflight
from rockycode.pricing import UsageLedger


def fresh():  # discovery is TTL-cached — tests mutate the stub, so drop it
    P._local_cache.clear()


# ── discovery: live list, keyless, configured without any key ────────────────
fresh()
provs = P.discover()
ol = provs["ollama"]
assert ol.local and ol.builtin and ol.reasoning == "none"
assert ol.models == STATE["models"], "picker shows what's ACTUALLY pulled"
assert ol.endpoints[0].keyless and ol.endpoints[0].key() == "local"
for v in list(os.environ):
    if v.startswith("ROCKYCODE_") and v.endswith("_API_KEY"):
        os.environ.pop(v, None)
ids = {c.id for c in P.configured_choices()}
assert "ollama:qwen3.8:27b-mlx" in ids, "keyless local = configured, no key needed"
assert onboarding.provider_key("") == "local", "keyless endpoint → placeholder key"
p, e, m = P.resolve("ollama")
assert e.eid == "ollama" and m == "qwen3.8:27b-mlx", "bare /model ollama → first pulled"
p, e, m = P.resolve("ollama:llama")
assert m == "llama3.2:3b"
# ollama tags CONTAIN a colon — the spec split must only eat the first one
# (regression: `ollama:qwen3.8:27b-mlx` once became model "qwen3.8 27b-mlx")
p, e, m = P.resolve("ollama:qwen3.8:27b-mlx")
assert m == "qwen3.8:27b-mlx", "full colon-tagged spec resolves exactly"
p, e, m = P.resolve("ollama qwen3.8:27b-mlx")
assert m == "qwen3.8:27b-mlx", "space form keeps the tag's colon too"
assert P.split_spec("ollama:qwen3.5:2b") == ("ollama", "qwen3.5:2b")
assert P.local_hints() == [], "server up + models pulled → no hint rows"
assert P.local_hint("ollama:nope") and "ollama pull nope" in P.local_hint("ollama:nope")
assert "ollama pull qwen3.5:2b" in P.local_hint("ollama:qwen3.5:2b"), \
    "the pull advice must keep the tag's colon — a mangled command helps no one"
print("discovery: live model list, keyless configured, resolve, pull-hint  ✓")

# ── preflight: ready — verified ctx from num_ctx, tools ok ───────────────────
pf = preflight(ROOT + "/v1", "qwen3.8:27b-mlx")
assert pf.ok and pf.ctx_verified and pf.context_window == 65536
assert any("tool calling" in c.text for c in pf.checks)
assert not pf.vision
print("preflight ready: server/model/tools/ctx all green, ctx 65,536 verified  ✓")

# ── preflight: /api/ps (loaded reality) outranks num_ctx; small ctx = FAIL ───
STATE["ps"] = [{"model": "qwen3.8:27b-mlx", "context_length": 4096}]
pf = preflight(ROOT + "/v1", "qwen3.8:27b-mlx")
assert not pf.ok and pf.context_window == 4096
assert any("OLLAMA_CONTEXT_LENGTH" in c.fix for c in pf.checks if c.status == "fail"), \
    "a silently-truncating ctx must block the switch WITH the exact fix"
STATE["ps"] = []
print("preflight ctx: loaded 4,096 → refused, OLLAMA_CONTEXT_LENGTH fix shown  ✓")

# ── preflight: unknown ctx → warn + safe default, never a false verdict ──────
STATE["parameters"] = "temperature 0.7"
pf = preflight(ROOT + "/v1", "qwen3.8:27b-mlx")
assert pf.ok and not pf.ctx_verified and pf.context_window == CTX_RECOMMENDED
assert any(c.status == "warn" and "verified" in c.text for c in pf.checks)
STATE["parameters"] = "num_ctx 65536\ntemperature 0.7"
print("preflight ctx unknown: warns + recommends, switch still allowed  ✓")

# ── preflight: model not pulled / no tool support → refused with the fix ─────
pf = preflight(ROOT + "/v1", "qwen3.8:9b")
assert not pf.ok and any("ollama pull qwen3.8:9b" in c.fix for c in pf.checks)
STATE["capabilities"] = ["completion"]
pf = preflight(ROOT + "/v1", "qwen3.8:27b-mlx")
assert not pf.ok and any("tool-calling" in c.text for c in pf.checks if c.status == "fail")
STATE["capabilities"] = ["completion", "tools", "vision"]
pf = preflight(ROOT + "/v1", "qwen3.8:27b-mlx")
assert pf.ok and pf.vision, "server-reported vision flips the eye on"
STATE["capabilities"] = ["completion", "tools"]
print("preflight: not-pulled and no-tools both refuse with exact commands  ✓")

# ── not running: hints everywhere, resolve None, preflight says how ──────────
os.environ["ROCKYCODE_OLLAMA_URL"] = DEAD
fresh()
assert P.discover()["ollama"].models == []
assert P.resolve("ollama") is None
assert any("not running" in h for h in P.local_hints())
hint = P.local_hint("ollama")
assert hint and "ollama serve" in hint
pf = preflight(DEAD + "/v1", "qwen3.8:27b-mlx")
assert not pf.ok and "ollama serve" in pf.checks[0].fix
os.environ["ROCKYCODE_OLLAMA_URL"] = ROOT
fresh()
print("not running: picker hint + /model hint + preflight all say `ollama serve`  ✓")

# ── engine launch straight onto local: keyless client, paced ctx ─────────────
from rockycode.engine.loop import Engine
eng = Engine(model="ollama", workdir=Path.cwd())
assert eng.provider_local and eng.provider_name == "ollama"
assert eng.model == "qwen3.8:27b-mlx"
assert eng.context_window == P.LOCAL_CONTEXT_WINDOW, \
    "the 1M DeepSeek default must not reach a local provider"
assert str(eng.client.base_url).startswith(ROOT), "client talks to the local server"
eng2 = Engine(model="ollama", workdir=Path.cwd(), context_window=32768)
assert eng2.context_window == 32768, "an explicit smaller setting is kept"
print("engine launch: keyless client built, context_window 65,536 not 1M  ✓")

# ── pricing: $0 by design, never the price nag ───────────────────────────────
led = UsageLedger()
led.mark_free("qwen3.8:27b-mlx")
led.add("qwen3.8:27b-mlx", {"prompt_tokens": 50_000, "completion_tokens": 9_000})
assert led.priced("qwen3.8:27b-mlx") and led.cost("usd") == 0.0 == led.cost("cny")
assert led.configured("usd") and led.configured("cny") and led.all_free()
assert led.rate("qwen3.8:27b-mlx")["out"] == 0.0, "never the fallback's rate"
led.add("deepseek-v4-flash", {"prompt_tokens": 1000, "completion_tokens": 10})
assert not led.all_free() and led.cost("usd") > 0, "mixed session prices the API part"
print("pricing: local $0 configured, mixed sessions stay honest  ✓")

# ── compaction summarize: local = no tool schemas, no reasoning fields ───────
from rockycode.engine import compaction

captured = {}


class _FakeCompletions:
    async def create(self, **kw):
        captured.clear(); captured.update(kw)
        msg = types.SimpleNamespace(content="state doc")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)],
                                     usage=None)


fake = types.SimpleNamespace(chat=types.SimpleNamespace(completions=_FakeCompletions()))
hist = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
s, u = asyncio.run(compaction.summarize(fake, "qwen3.8:27b-mlx", hist, tools=[],
                                        reasoning="none"))
assert s == "state doc"
assert "tools" not in captured and "tool_choice" not in captured, \
    "local summarize must not carry tool schemas (tool_choice is ignored there)"
assert "extra_body" not in captured, "reasoning none → clean body"
s, u = asyncio.run(compaction.summarize(fake, "deepseek-v4-pro", hist,
                                        tools=[{"type": "function"}]))
assert captured["tool_choice"] == "none" and captured["tools"], "cloud shape unchanged"
assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
print("compaction: local summarize drops tools+reasoning, deepseek shape intact  ✓")

# ── TUI: the /model switch is preflight-GATED — refuse, teach, then switch ───
os.environ["ROCKYCODE_API_KEY"] = "sk-rocky-test"  # for the switch-back
from textual.widgets import Static

from rockycode.tui.app import RockyCodeApp


async def tui_flow():
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace()))
    eng = Engine(model="deepseek-v4-pro", client=client, workdir=Path.cwd())
    app = RockyCodeApp(eng, permission="yolo")
    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()

        def transcript():
            return "\n".join(str(w.render()) for w in app.query(Static))

        # not ready (no tool support) → REFUSED, engine untouched, fix on screen
        STATE["capabilities"] = ["completion"]
        fresh()
        await app._handle_model("/model ollama")
        await pilot.pause()
        assert eng.provider_name == "deepseek" and eng.model == "deepseek-v4-pro", \
            "a failed preflight must leave the engine untouched"
        assert eng.context_window == 1_048_576, "ctx untouched on refusal"
        out = transcript()
        assert "not ready" in out and "qwen3.8:27b-mlx" in out
        assert "tool-calling" in out, "the failing check is named"

        # fixed → preflight passes, switch lands, ctx paced to verified 65,536
        STATE["capabilities"] = ["completion", "tools"]
        fresh()
        await app._handle_model("/model ollama")
        await pilot.pause()
        assert eng.provider_name == "ollama" and eng.model == "qwen3.8:27b-mlx"
        assert eng.provider_local and eng.context_window == 65536
        assert "qwen3.8:27b-mlx" in app._ledger.free_models, "$0 · local, no price nag"
        out = transcript()
        assert "preflight" in out and "context_window → 65,536" in out
        assert "$0 API fee" in out

        # back to cloud → the pre-local context_window is restored
        await app._handle_model("/model deepseek")
        await pilot.pause()
        assert eng.provider_name == "deepseek" and not eng.provider_local
        assert eng.context_window == 1_048_576, "cloud ctx restored after local"


asyncio.run(tui_flow())
print("TUI gate: refuse-with-fix → switch → ctx paced 65,536 → restored on cloud  ✓")

srv.shutdown()
print("\nsmoke_local_models: all green")
