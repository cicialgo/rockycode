"""Provider + model registry: models are DATA, no adapter glue.

Covers the registry loaded from rockycode/models.toml (cn-only endpoints,
plan rows, key aliases, legacy id aliases), `/model` spec resolution, the
per-wire-shape extra_body, rocky-owned keys, and the engine's live provider
switch adopting a ModelSpec. No network, no real client.
"""
import os
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

# a dead port: this test's counts must not shift with whatever the dev's own
# ollama has pulled (smoke_local_models covers the live-discovery behavior)
os.environ["ROCKYCODE_OLLAMA_URL"] = "http://127.0.0.1:9"
os.environ.setdefault("ROCKYCODE_HOME", tempfile.mkdtemp(prefix="rockytest-home-"))

from rockycode.engine import providers as P
from rockycode.engine import route as route_mod
from rockycode.engine.effort import build_extra_body
from rockycode.engine.loop import Engine
from rockycode import onboarding

# ── registry: the 2026-09 roster, one cn endpoint each, plan rows, aliases ──
provs = P.discover()
for name in ("deepseek", "glm", "kimi", "minimax", "stepfun", "qwen", "mimo", "openrouter", "ollama"):
    assert name in provs, (name, list(provs))
ds = provs["deepseek"]
assert ds.endpoints[0].key_env == onboarding.KEY_ENV, "deepseek → existing ROCKYCODE_API_KEY"
assert ds.models == ["deepseek-flash", "deepseek-v4-pro"], ds.models
assert "deepseek-v4-flash-vision-exp" not in ds.models, "vision-exp is retired from the list"
eids = {e.eid: e for pr in provs.values() for e in pr.endpoints}
assert eids["kimi"].key_env == "ROCKYCODE_KIMI_API_KEY"
assert "ROCKYCODE_KIMI_CN_API_KEY" in eids["kimi"].key_aliases, "old regional key name still read"
assert "kimi-cn" not in eids and "kimi-en" not in eids, "no en/cn split anymore"
assert eids["glm"].base_url.startswith("https://open.bigmodel.cn/")
assert eids["minimax"].base_url == "https://api.minimax.cn/v1"
# subscription plans are their own endpoint rows with their own key
assert eids["qwen-plan"].key_env == "ROCKYCODE_QWEN_PLAN_API_KEY" and "token-plan" in eids["qwen-plan"].base_url
assert "mimo-plan" in eids and "stepfun-plan" in eids
assert eids["qwen-plan"].label, "plan rows carry a label for the picker"
assert provs["glm"].models == ["glm-5.3", "glm-5.3-flash"]
assert provs["qwen"].models == ["qwen3.8-max", "qwen3.8-flash", "qwen4-max"]
assert provs["mimo"].models == ["mimo-v2.6-pro"] and provs["stepfun"].models == ["step-5-preview"]
orp = provs["openrouter"]
assert orp.endpoints[0].base_url == "https://openrouter.ai/api/v1"
assert orp.endpoints[0].key_env == "ROCKYCODE_OPENROUTER_API_KEY"
assert orp.endpoints[0].key_aliases == (), "verifier OPENROUTER_API_KEY is not a chat-key alias"
assert orp.reasoning == "none" and orp.cache_field == "cached_tokens"
assert orp.models == ["openrouter-free"], orp.models
free = orp.spec("openrouter-free")
assert free.wire == "openrouter/free" and free.vision and free.context == 65_536 and free.max_output == 8_192
assert not free.price and not free.roles and free.protocol == P.GENERIC
assert build_extra_body(True, "max", orp.reasoning) == {}
assert build_extra_body(False, "off", orp.reasoning) == {}
assert P.role_model("default") == "deepseek-flash", "openrouter must not take the home role"
print("registry: 8 cloud providers + ollama · cn endpoints · openrouter free · plan rows · key aliases  ✓")

# ── wire shapes + tiers + cache field are registry data ──────────────────────
assert ds.reasoning == "thinking" and ds.efforts == ("low", "high", "max") and ds.thinking_off
assert ds.cache_field == "prompt_cache_hit_tokens" and ds.search == "anthropic" and ds.peak == "deepseek"
assert provs["glm"].reasoning == "thinking" and not provs["glm"].thinking_off
assert provs["kimi"].reasoning == "effort" and not provs["kimi"].thinking_off
assert provs["minimax"].reasoning == "minimax" and provs["minimax"].cache_field == "cached_tokens"
assert provs["qwen"].reasoning == "enable_thinking"
assert provs["stepfun"].reasoning == "openai" and provs["stepfun"].efforts == ("low", "medium", "high")
assert provs["mimo"].reasoning == "thinking" and provs["mimo"].efforts == (), "mimo: no effort field"
# PR #89's useful guards belong in this CI-collected registry/request suite.
# A Harness-specific default must not silently force either general API on.
for name, model in (("deepseek", "deepseek-flash"), ("mimo", "mimo-v2.6-pro")):
    provider = provs[name]
    assert provider.thinking_off_for(model)
    assert build_extra_body(False, "low", provider.reasoning,
        efforts=provider.efforts_for(model), thinking_off=provider.thinking_off_for(model)) == {
            "thinking": {"type": "disabled"}}
for effort in ("low", "high", "max"):
    assert build_extra_body(True, effort, provs["mimo"].reasoning,
        efforts=provs["mimo"].efforts_for("mimo-v2.6-pro")) == {"thinking": {"type": "enabled"}}
with patch.dict(os.environ, {"ROCKYCODE_MIMO_API_KEY": "fixture-paygo",
                             "ROCKYCODE_MIMO_PLAN_API_KEY": "fixture-plan"}):
    for eid, url, key_env, key in (
            ("mimo", "https://api.xiaomimimo.com/v1", "ROCKYCODE_MIMO_API_KEY", "fixture-paygo"),
            ("mimo-plan", "https://token-plan-cn.xiaomimimo.com/v1", "ROCKYCODE_MIMO_PLAN_API_KEY", "fixture-plan")):
        provider, endpoint, model = P.resolve(f"{eid}:mimo-v2.6-pro")
        assert model == provider.api_model(model) == "mimo-v2.6-pro"
        assert endpoint.base_url == url and endpoint.key_env == key_env and endpoint.key() == key
        assert provider.spec(model).protocol.label == ("replay_reasoning=always strict_finish "
                                                       "tool_choice_none=false output_limit=max_completion_tokens")
    with patch.dict(os.environ, {"ROCKYCODE_MIMO_PLAN_API_KEY": ""}):
        assert P.resolve("mimo-plan:mimo-v2.6-pro")[1].key() is None, "no Paygo key fallback"
print("shapes: thinking/effort/enable_thinking/minimax/openai + tiers + cache field per provider  ✓")

# ── specs: limits, vision, price, roles per MODEL ────────────────────────────
flash = ds.spec("deepseek-flash")
assert flash.context == 1_048_576 and flash.max_output == 393_216 and flash.vision
assert flash.price["usd"]["in_miss"] == 0.15 and flash.price["cny"]["out"] == 4.0
assert {"default", "sidecar", "search"} <= set(flash.roles)
assert not ds.spec("deepseek-v4-pro").vision
assert provs["glm"].spec("glm-5.3-flash").vision and not provs["glm"].spec("glm-5.3").vision
assert provs["kimi"].spec("kimi-k3").max_output == 131_072
assert provs["stepfun"].spec("step-5-preview").max_output == 65_536
assert not provs["kimi"].spec("kimi-k3").price, "unverified prices are OMITTED, never guessed"
assert P.role_model("search") == "deepseek-flash" and P.role_model("sidecar") == "deepseek-flash"
unknown = ds.spec("some-future-model")
assert unknown.context == P.DEFAULT_CONTEXT and unknown.max_output == P.DEFAULT_MAX_OUTPUT
print("specs: per-model ctx/out/vision/price/roles; unknown model → generous defaults  ✓")

# ── resolve: endpoint / endpoint:model / bare unique / legacy alias ──────────
p, e, m = P.resolve("deepseek:flash")
assert e.eid == "deepseek" and m == "deepseek-flash", (e.eid, m)
p, e, m = P.resolve("deepseek")
assert m == "deepseek-flash", "bare provider → its first (default) model"
p, e, m = P.resolve("kimi")
assert e.eid == "kimi" and m == "kimi-k3", (e.eid, m)
p, e, m = P.resolve("glm:flash")
assert m == "glm-5.3-flash"
p, e, m = P.resolve("glm:5.3")
assert m == "glm-5.3", "base model wins its own substring vs -flash"
p, e, m = P.resolve("m3")  # unique bare model id — the shorter base wins over m3.1-flash
assert e.eid == "minimax" and m == "minimax-m3", e.eid
p, e, m = P.resolve("minimax-m3.1-flash")
assert e.eid == "minimax" and m == "minimax-m3.1-flash" and e.base_url == "https://api.minimax.cn/v1"
assert P.resolve("MiniMax-M3.1-Flash")[2] == "minimax-m3.1-flash", "docs' casing resolves to the friendly id"
assert P.resolve("m3.1")[2] == "minimax-m3.1-flash"
flash_mm = provs["minimax"].spec("minimax-m3.1-flash")
assert flash_mm.wire == "MiniMax-M3.1-Flash"
assert flash_mm.context == 1_000_000 and flash_mm.max_output == 131_072 and flash_mm.vision
assert flash_mm.thinking_off is False
assert flash_mm.efforts == ("low", "medium", "high", "xhigh", "max")
assert flash_mm.price["usd"] == {"in_hit": 0.015, "in_miss": 0.15, "out": 0.6}
assert flash_mm.price["cny"]["in_hit"] == 0.1 and flash_mm.price["cny"]["in_miss"] == 1.0
assert flash_mm.price["cny"]["out"] == 4.0
assert provs["minimax"].api_model("minimax-m3") == "minimax-m3"
assert provs["minimax"].api_model("minimax-m3.1-flash") == "MiniMax-M3.1-Flash"
assert provs["minimax"].thinking_off_for("minimax-m3") is True
assert provs["minimax"].thinking_off_for("minimax-m3.1-flash") is False
assert provs["minimax"].efforts_for("minimax-m3") is None
assert provs["minimax"].models[0] == "minimax-m3", "bare /model minimax stays on M3"
p, e, m = P.resolve("qwen-plan:max")
assert e.eid == "qwen-plan" and m == "qwen3.8-max"
# legacy DeepSeek ids (retired 2026-09-10) keep resolving — DeepSeek serves
# them with V4.1-Flash, and so does rocky's registry
assert P.resolve("deepseek-v4-flash")[2] == "deepseek-flash"
assert P.resolve("deepseek-v4-flash-vision-exp")[2] == "deepseek-flash"
assert P.resolve("deepseek:vision-exp")[2] == "deepseek-flash", "alias substring too"
assert P.resolve("deepseek:pro")[2] == "deepseek-v4-pro"
p, e, m = P.resolve("openrouter")
assert e.eid == "openrouter" and m == "openrouter-free", (e.eid, m)
assert P.resolve("openrouter/free")[2] == "openrouter-free", "docs' router id resolves"
assert P.resolve("openrouter-laguna-s") is None, "a model that left the catalog is not offered"
assert P.resolve("nope") is None
assert P.resolve("kimi-xx") is None, "unknown endpoint id → None"
assert P.resolve("glm:nope") is None, "unknown model substring → None"
print("resolve: endpoint / endpoint:model / bare-unique / plan / legacy alias / unknown→None  ✓")

# ── configured picker stays SHORT: only keyed choices (+ deepseek always) ─────
for v in list(os.environ):
    if v.startswith("ROCKYCODE_") and v.endswith("_API_KEY") and v != onboarding.KEY_ENV:
        os.environ.pop(v, None)
short = P.configured_choices()
assert all(c.provider.name == "deepseek" for c in short), \
    "with no provider keys, the picker shows only deepseek"
os.environ["ROCKYCODE_KIMI_CN_API_KEY"] = "sk-rocky-kimi-cn"  # the OLD name — an alias
short = P.configured_choices()
ids = {c.id for c in short}
assert "kimi:kimi-k3" in ids, ids
assert not any(c.prov_id == "minimax" for c in short), "unkeyed endpoints stay hidden"
assert len(short) < len(P.choices()), "configured list is shorter than the full catalog"
kimi_c = [c for c in short if c.prov_id == "kimi"][0]
assert kimi_c.endpoint.key_source() == "ROCKYCODE_KIMI_CN_API_KEY", "picker can name the alias in use"
os.environ.pop("ROCKYCODE_KIMI_CN_API_KEY", None)
os.environ["OPENROUTER_API_KEY"] = "sk-or-ambient"  # verifier key — must not reveal the chat rows
short = P.configured_choices()
assert not any(c.prov_id == "openrouter" for c in short), "ambient OPENROUTER_API_KEY is not the chat key"
os.environ.pop("OPENROUTER_API_KEY", None)
os.environ["ROCKYCODE_OPENROUTER_API_KEY"] = "sk-or-rocky"
short = P.configured_choices()
ids = {c.id for c in short}
assert "openrouter:openrouter-free" in ids, ids
assert not any(c.provider.name == "glm" for c in short), "a free key must not reveal other providers"
os.environ.pop("ROCKYCODE_OPENROUTER_API_KEY", None)
print("picker: configured-only (short) vs full catalog; an aliased old key still reveals kimi  ✓")

# ── effort: the reasoning-param SHAPE is per wire shape, not per adapter ─────
assert build_extra_body(True, "max", "deepseek") == {
    "thinking": {"type": "enabled"}, "reasoning_effort": "max"}
assert build_extra_body(True, "xhigh", "openai") == {"reasoning_effort": "high"}, \
    "openai shape: bare reasoning_effort, legacy xhigh = max = top tier"
assert build_extra_body(True, "high", "openai") == {"reasoning_effort": "medium"}
assert build_extra_body(True, "max", "none") == {}, "none shape: no reasoning fields"
assert build_extra_body(False, "max", "deepseek") == {"thinking": {"type": "disabled"}}
assert build_extra_body(False, "max", "openai") == {}
assert build_extra_body(False, "max", "thinking", thinking_off=False) == {
    "thinking": {"type": "enabled"}, "reasoning_effort": "low"}, "glm: off = lowest tier"
assert build_extra_body(True, "high", "effort") == {"reasoning_effort": "high"}, "kimi"
assert build_extra_body(True, "max", "enable_thinking") == {"enable_thinking": True}, "qwen"
assert build_extra_body(True, "max", "minimax") == {"thinking": {"type": "adaptive"}, "reasoning_split": True}
assert build_extra_body(False, "max", "minimax") == {"thinking": {"type": "disabled"}}, "M3 can still disable"
_mm_tiers = ("low", "medium", "high", "xhigh", "max")
assert build_extra_body(True, "max", "minimax", efforts=_mm_tiers, thinking_off=False) == {
    "thinking": {"type": "adaptive"}, "reasoning_split": True, "reasoning_effort": "max"}
assert build_extra_body(True, "high", "minimax", efforts=_mm_tiers, thinking_off=False)["reasoning_effort"] == "high"
assert build_extra_body(True, "low", "minimax", efforts=_mm_tiers, thinking_off=False)["reasoning_effort"] == "low"
assert build_extra_body(False, "max", "minimax", efforts=_mm_tiers, thinking_off=False) == {
    "thinking": {"type": "adaptive"}, "reasoning_split": True, "reasoning_effort": "low"}, \
    "M3.1-Flash: dial off must not send thinking disabled"
assert build_extra_body(True, "max", "thinking", efforts=()) == {"thinking": {"type": "enabled"}}, "mimo: no effort field"
print("effort: six wire shapes, thinking-off per shape, tiers clamped by position  ✓")

# ── per-provider key: rocky-owned only, never ambient; aliases honored ──────
for v in ("ROCKYCODE_MINIMAX_API_KEY", "ROCKYCODE_MINIMAX_CN_API_KEY", "MINIMAX_API_KEY"):
    os.environ.pop(v, None)
os.environ["MINIMAX_API_KEY"] = "sk-users-own-minimax"  # ambient — must be ignored
try:
    route_mod.route("minimax-m3")
    raise AssertionError("must not fall back to the ambient MINIMAX_API_KEY")
except route_mod.RouteError as e:
    assert "ROCKYCODE_MINIMAX_API_KEY" in str(e) and e.key_env == "ROCKYCODE_MINIMAX_API_KEY"
os.environ["ROCKYCODE_MINIMAX_CN_API_KEY"] = "sk-rocky-minimax-old-name"
_old = route_mod.route("minimax-m3")
assert _old.key == "sk-rocky-minimax-old-name", "alias name still read"
assert _old.key_env == "ROCKYCODE_MINIMAX_CN_API_KEY", "and named as the source"
os.environ.pop("ROCKYCODE_MINIMAX_CN_API_KEY", None); os.environ.pop("MINIMAX_API_KEY", None)
print("key: rocky-owned ROCKYCODE_<P>_API_KEY (+ old aliases) only, ambient provider key ignored  ✓")

# ── engine: live provider switch swaps client+model+shape, not history ──────
client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace()))
eng = Engine(model="deepseek-v4-pro", client=client, workdir=Path(tempfile.mkdtemp()),
             system_prompt="BASE")
assert eng.reasoning_policy == "thinking" and eng.provider_name == "deepseek"
assert eng._extra_body() == {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}
before = list(eng.history)
new_client = types.SimpleNamespace(chat="minimax-client")
eng.switch_provider(new_client, "minimax-m3", provider_name="minimax", reasoning_policy="minimax")
assert eng.client is new_client and eng.model == "minimax-m3"
assert eng.provider_name == "minimax" and eng.reasoning_policy == "minimax"
assert eng._extra_body() == {"thinking": {"type": "adaptive"}, "reasoning_split": True}, eng._extra_body()
assert eng.history == before, "a provider switch must not touch conversation history"
mm = provs["minimax"]
eng.switch_provider(new_client, "minimax-m3.1-flash", provider_name="minimax",
                    reasoning_policy=mm.reasoning, provider=mm, endpoint=mm.endpoints[0])
assert eng.model == "minimax-m3.1-flash" and eng.wire_model == "MiniMax-M3.1-Flash"
assert eng.context_window == 1_000_000 and eng.max_tokens == 131_072
assert eng.vision_enabled and not eng.thinking_off
assert eng._extra_body() == {
    "thinking": {"type": "adaptive"}, "reasoning_split": True, "reasoning_effort": "max"}
eng.thinking = False
assert "disabled" not in str(eng._extra_body()) and eng._extra_body()["reasoning_effort"] == "low"
eng.thinking = True
# a registry-backed switch ADOPTS the spec: tiers, thinking_off, cache field, limits
glm, glm_ep = provs["glm"], provs["glm"].endpoints[0]
eng.switch_provider(new_client, "glm-5.3", provider_name="glm", reasoning_policy=glm.reasoning,
                    provider=glm, endpoint=glm_ep)
assert eng.max_tokens == 131_072 and eng.context_window == 1_048_576, (eng.max_tokens, eng.context_window)
assert not eng.thinking_off and eng.cache_field == "cached_tokens"
eng.thinking = False
assert eng._extra_body() == {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}, "glm off = low"
eng.thinking = True
kimi, kimi_ep = provs["kimi"], provs["kimi"].endpoints[0]
eng.switch_provider(new_client, "kimi-k3", provider_name="kimi", reasoning_policy=kimi.reasoning,
                    provider=kimi, endpoint=kimi_ep)
assert eng.vision_enabled and eng._extra_body() == {"reasoning_effort": "max"}
print("engine: switch swaps client+model+shape, adopts spec limits, history untouched  ✓")

# ── limits: explicit numbers are pinned across switches; 0 unpins ───────────
pinned = Engine(model="x", client=client, workdir=Path(tempfile.mkdtemp()), max_tokens=5000)
pinned.adopt(glm, glm_ep, "glm-5.3")
assert pinned.max_tokens == 5000 and pinned.context_window == 1_048_576, "pinned cap kept, window follows"
pinned.set_limit("max_tokens", 0)
assert pinned.max_tokens == 131_072, "unpinned → the active model's registry cap"
pinned.set_limit("context_window", 200_000)
pinned.adopt(ds, ds.endpoints[0], "deepseek-flash")
assert pinned.context_window == 200_000 and pinned.max_tokens == 393_216
print("limits: --max-tokens/--context-window pin; 0 follows the model  ✓")

# ── model-level vision: deepseek-flash sees, pro doesn't, one key ────────────
by_model = {c.model: c for c in P.choices() if c.provider.name == "deepseek"}
assert by_model["deepseek-flash"].vision and not by_model["deepseek-v4-pro"].vision
assert not ds.vision, "provider-level flag stays off — vision is per model"
print("vision: per-MODEL flag — flash ❖, pro text  ✓")

# ── config vision_models: "pro gained vision" is a config flip, not code ────
from rockycode import config as C
C.GLOBAL_PATH = Path(tempfile.mkdtemp()) / "config.toml"  # never the real ~/.rockycode
C.set_value("vision_models", "deepseek-v4-pro, no-such-model")
flip = {c.model: c for c in P.choices() if c.provider.name == "deepseek"}
assert flip["deepseek-v4-pro"].vision, "config vision_models marks pro as seeing"
assert "no-such-model" not in P.discover()["deepseek"].vision_models, "unknown ids ignored"
C.set_value("vision_models", "")
assert not [c for c in P.choices() if c.provider.name == "deepseek" and
            c.model == "deepseek-v4-pro"][0].vision, "cleared → pro text-only again"
print("config: vision_models flips a model's sight from any shell — no code change  ✓")

# ── endpoints.toml: an own base URL becomes a first-class <provider>-custom ──
eid, err = P.set_custom_url("kimi", "https://my-gateway.example/v1")
assert err is None and eid == "kimi-custom"
kimi = P.discover()["kimi"]
custom = [e for e in kimi.endpoints if e.eid == "kimi-custom"]
assert custom and custom[0].base_url == "https://my-gateway.example/v1"
assert custom[0].key_env == kimi.endpoints[0].key_env, "custom URL rides the provider's key"
assert custom[0].key_aliases == kimi.endpoints[0].key_aliases, "…and its aliases"
p, e, m = P.resolve("kimi-custom:kimi-k3")
assert e.eid == "kimi-custom" and m == "kimi-k3", "custom endpoint is /model-addressable"
_eid, err = P.set_custom_url("kimi", "ftp://nope")
assert err is not None, "non-http url refused"
_eid, err = P.set_custom_url("kimi", "")  # empty removes the entry
assert err is None and not [e for e in P.discover()["kimi"].endpoints if e.eid == "kimi-custom"]
print("endpoints.toml: own gateway URL per provider — added, resolvable, removable  ✓")

# ── fee alignment: peak follows the provider's schedule; unpriced stays honest ─
from datetime import datetime, timezone
from rockycode.pricing import UsageLedger

led = UsageLedger()
assert not led.priced("minimax-m3"), "provider model has no built-in rate"
led.add("minimax-m3", {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000})
assert led.cost("usd") == 0.0, "unpriced model must not read as DeepSeek dollars"
assert not led.configured("usd"), "unpriced usage flags '(prices unset)'"
peak_at = datetime(2026, 8, 3, 2, 0, tzinfo=timezone.utc)  # Monday, inside 01:00–04:00
dsl = UsageLedger(); dsl.add("deepseek-flash", {"prompt_tokens": 1_000_000}, at=peak_at)
mm = UsageLedger(); mm.add("minimax-m3", {"prompt_tokens": 1_000_000}, at=peak_at)
assert any(peak for (_m, peak) in dsl.buckets), "deepseek turn is peak-bucketed in-window"
assert not any(peak for (_m, peak) in mm.buckets), "minimax turn is NEVER peak-multiplied"
priced = UsageLedger()
assert priced.priced("minimax-m3.1-flash")
priced.add("minimax-m3.1-flash", {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}, at=peak_at)
assert abs(priced.cost("usd") - 0.75) < 1e-9, priced.cost("usd")
assert abs(priced.cost("cny") - 5.0) < 1e-9, priced.cost("cny")
assert not any(peak for (_m, peak) in priced.buckets), "flash is not on the deepseek peak schedule"
cached = UsageLedger()
cached.add("minimax-m3.1-flash",
           {"prompt_tokens": 1_000_000, "prompt_cache_hit_tokens": 1_000_000, "completion_tokens": 0})
assert abs(cached.cost("usd") - 0.015) < 1e-9, cached.cost("usd")
assert abs(cached.cost("cny") - 0.10) < 1e-9, cached.cost("cny")
print("fee: peak is a per-provider schedule (deepseek only); unpriced models flag unset  ✓")

# ── the chat call sends the docs' model id, not the friendly /model token ──
import asyncio


class _Rec:
    seen: dict = {}

    async def create(self, **kw):
        _Rec.seen = kw

        async def stream():
            delta = types.SimpleNamespace(reasoning_content=None, content="ok", tool_calls=None)
            yield types.SimpleNamespace(usage=None, choices=[types.SimpleNamespace(delta=delta, finish_reason="stop")])
        return stream()


rec_client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=_Rec()))
wired = Engine(model="x", client=rec_client, workdir=Path(tempfile.mkdtemp()), registry={})
_mm = provs["minimax"]
wired.switch_provider(rec_client, "minimax-m3.1-flash", provider_name="minimax",
                      reasoning_policy=_mm.reasoning, provider=_mm, endpoint=_mm.endpoints[0])


async def _one_turn():
    async for _ev in wired.run_turn("hi"):
        pass


asyncio.run(_one_turn())
assert _Rec.seen["model"] == "MiniMax-M3.1-Flash", _Rec.seen.get("model")
assert _Rec.seen["extra_body"]["reasoning_effort"] == "max"
assert _Rec.seen["extra_body"]["thinking"] == {"type": "adaptive"}
assert _Rec.seen["stream"] is True and _Rec.seen["stream_options"] == {"include_usage": True}
print("wire: friendly minimax-m3.1-flash → API model MiniMax-M3.1-Flash  ✓")

print("PROVIDERS SMOKE OK — models are data, the SDK is the only glue. amaze!")
