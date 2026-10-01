"""The model registry as DATA: rockycode/models.toml + the user's override.

Covers: the override deep-merge (add a model, hide a builtin, correct a
limit, add a price), add_provider's write→reload roundtrip, the usage cache
field normalizer, role lookups, and token formatting. No network.
"""
import os
import tempfile
from pathlib import Path

os.environ["ROCKYCODE_OLLAMA_URL"] = "http://127.0.0.1:9"
os.environ.setdefault("ROCKYCODE_HOME", tempfile.mkdtemp(prefix="rockytest-home-"))

from rockycode.engine import providers as P

home = Path(tempfile.mkdtemp(prefix="rockyreg-"))
P.MODELS_TOML = home / "models.toml"
P.ENDPOINTS_TOML = home / "endpoints.toml"
P.PROVIDERS_TOML = home / "providers.toml"

# ── builtin file: well-formed, every provider has ≥1 model, every model a provider ─
data = P.registry_data()
assert data.get("schema") == 1
cat = P.catalog()
assert set(cat) == {"deepseek", "glm", "kimi", "minimax", "stepfun", "qwen", "mimo"}, set(cat)
for name, prov in cat.items():
    assert prov.models, f"{name} lists no models"
    assert prov.reasoning in P.REASONING_SHAPES, (name, prov.reasoning)
    for ep in prov.endpoints:
        assert ep.base_url.startswith("https://"), (name, ep.base_url)
        assert ep.key_env.startswith("ROCKYCODE_"), (name, ep.key_env)
    for m in prov.models:
        sp = prov.spec(m)
        assert sp.context >= 65_536 and 1024 <= sp.max_output <= sp.context, (m, sp)
print("builtin models.toml: every row well-formed  ✓")

# ── user override: deep-merge — add, hide, correct, price ────────────────────
P.MODELS_TOML.write_text('''
[models."deepseek-v4-pro"]
hidden = true

[models."kimi-k3"]
max_output = 262144
[models."kimi-k3".price]
usd = { in_hit = 0.1, in_miss = 0.5, out = 2.0 }

[models."my-finetune"]
provider = "qwen"
context = 131072
max_output = 8192
vision = false

[providers.mybox]
label = "my vLLM box"
base_url = "https://box.example/v1"
reasoning = "none"
[models."box-7b"]
provider = "mybox"
context = 32768
max_output = 4096
''')
cat = P.catalog()
assert "deepseek-v4-pro" not in cat["deepseek"].models and cat["deepseek"].models == ["deepseek-flash"]
assert cat["kimi"].spec("kimi-k3").max_output == 262_144, "one field corrected, the rest kept"
assert cat["kimi"].spec("kimi-k3").context == 1_048_576 and cat["kimi"].spec("kimi-k3").vision
assert cat["kimi"].spec("kimi-k3").price["usd"]["out"] == 2.0
assert "my-finetune" in cat["qwen"].models and cat["qwen"].spec("my-finetune").max_output == 8192
assert cat["mybox"].endpoints[0].key_env == "ROCKYCODE_MYBOX_API_KEY" and cat["mybox"].models == ["box-7b"]
assert P.resolve("mybox")[2] == "box-7b" and P.resolve("deepseek:pro") is None
from rockycode.pricing import UsageLedger
led = UsageLedger()
assert led.priced("kimi-k3"), "a price added in the override reaches the ledger"
led.add("kimi-k3", {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000})
assert abs(led.cost("usd") - 2.5) < 1e-9, led.cost("usd")
print("override: hide a builtin · correct a limit · add a price · add a model · add a provider  ✓")

# ── add_provider: rocky writes the override, it reloads identically ──────────
P.MODELS_TOML.unlink()
name, err = P.add_provider("Lab-Box", "http://lab:8000/v1", ["lab-70b", "lab-7b"],
                           reasoning="none", vision=True, context=65536, max_output=8192)
assert err is None and name == "lab-box", (name, err)
name2, err = P.add_provider("lab-box", "http://lab:8000/v1", ["lab-70b"])  # replace drops strays
assert err is None
cat = P.catalog()
assert cat["lab-box"].models == ["lab-70b"], cat["lab-box"].models
assert cat["lab-box"].reasoning == "openai" and cat["lab-box"].endpoints[0].base_url == "http://lab:8000/v1"
_n, err = P.add_provider("bad", "ftp://x", ["m"])
assert err and "http" in err
_n, err = P.add_provider("bad", "https://x/v1", [])
assert err and "model" in err
_n, err = P.add_provider("bad", "https://x/v1", ["m"], reasoning="magic")
assert err and "reasoning" in err
import tomllib
tomllib.loads(P.MODELS_TOML.read_text())  # the emitted file is valid TOML
assert "sk-" not in P.MODELS_TOML.read_text()
print("add_provider: validated, emitted as TOML, reloads; never a key  ✓")

# ── cache field normalizer + roles + formatting ──────────────────────────────
assert P.cache_hit_tokens({"prompt_cache_hit_tokens": 640}, "prompt_cache_hit_tokens") == 640
assert P.cache_hit_tokens({"prompt_tokens_details": {"cached_tokens": 128}}, "cached_tokens") == 128
assert P.cache_hit_tokens({"prompt_tokens": 5}, "cached_tokens") is None
assert P.cache_hit_tokens({"prompt_cache_hit_tokens": True}, "prompt_cache_hit_tokens") is None
assert P.role_model("search") == "deepseek-flash" and P.role_model("nope") is None
for v in list(os.environ):
    if v.startswith("ROCKYCODE_") and v.endswith("_API_KEY"):
        os.environ.pop(v, None)
assert P.role_choice("sidecar") is None and P.sidecar_client() is None, "no key → no sidecar"
os.environ["ROCKYCODE_API_KEY"] = "sk-rocky-home"
assert P.role_choice("sidecar").id == "deepseek:deepseek-flash"
os.environ.pop("ROCKYCODE_API_KEY")
assert P.fmt_tokens(1_048_576) == "1M" and P.fmt_tokens(384_000) == "384K"
assert P.fmt_tokens(65_536) == "64K" and P.fmt_tokens(131_072) == "128K" and P.fmt_tokens(1_000_000) == "1M"
print("cache field normalizer · roles · fmt_tokens  ✓")

# ── the engine reads the spec: cache normalization + limits at launch ────────
import asyncio
import types
from rockycode.engine.loop import Engine


class _Usage:
    def model_dump(self):
        return {"prompt_tokens": 1000, "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 640}}


class _FC:
    async def create(self, **kw):
        async def stream():
            d = types.SimpleNamespace(reasoning_content=None, content="ok", tool_calls=None)
            yield types.SimpleNamespace(usage=_Usage(), choices=[types.SimpleNamespace(delta=d)])
        return stream()


client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=_FC()))
eng = Engine(model="x", client=client, workdir=Path(tempfile.mkdtemp()))
kimi = P.discover()["kimi"]
eng.adopt(kimi, kimi.endpoints[0], "kimi-k3")
assert eng.cache_field == "cached_tokens"
seen = {}


async def run():
    async for ev in eng.run_turn("hi"):
        if type(ev).__name__ == "TurnFinished":
            seen.update(ev.usage)

asyncio.run(run())
assert seen.get("prompt_cache_hit_tokens") == 640, seen
print("engine: OpenAI-style cached_tokens lands as prompt_cache_hit_tokens for the ledger  ✓")

print("MODELS REGISTRY SMOKE OK — models are data. amaze!")
