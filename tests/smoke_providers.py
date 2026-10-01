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

# a dead port: this test's counts must not shift with whatever the dev's own
# ollama has pulled (smoke_local_models covers the live-discovery behavior)
os.environ["ROCKYCODE_OLLAMA_URL"] = "http://127.0.0.1:9"
os.environ.setdefault("ROCKYCODE_HOME", tempfile.mkdtemp(prefix="rockytest-home-"))

from rockycode.engine import providers as P
from rockycode.engine.effort import build_extra_body
from rockycode.engine.loop import Engine
from rockycode import onboarding

# ── registry: the 2026-09 roster, one cn endpoint each, plan rows, aliases ──
provs = P.discover()
for name in ("deepseek", "glm", "kimi", "minimax", "stepfun", "qwen", "mimo", "ollama"):
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
assert eids["minimax"].base_url == "https://api.minimaxi.com/v1"
# subscription plans are their own endpoint rows with their own key
assert eids["qwen-plan"].key_env == "ROCKYCODE_QWEN_PLAN_API_KEY" and "token-plan" in eids["qwen-plan"].base_url
assert "mimo-plan" in eids and "stepfun-plan" in eids
assert eids["qwen-plan"].label, "plan rows carry a label for the picker"
assert provs["glm"].models == ["glm-5.3", "glm-5.3-flash"]
assert provs["qwen"].models == ["qwen3.8-max", "qwen3.8-flash"]
assert provs["mimo"].models == ["mimo-v2.6-pro"] and provs["stepfun"].models == ["step-5-preview"]
print("registry: 7 cloud providers + ollama · cn endpoints · plan rows · key aliases  ✓")

# ── wire shapes + tiers + cache field are registry data ──────────────────────
assert ds.reasoning == "thinking" and ds.efforts == ("low", "high", "max") and ds.thinking_off
assert ds.cache_field == "prompt_cache_hit_tokens" and ds.search == "anthropic" and ds.peak == "deepseek"
assert provs["glm"].reasoning == "thinking" and not provs["glm"].thinking_off
assert provs["kimi"].reasoning == "effort" and not provs["kimi"].thinking_off
assert provs["minimax"].reasoning == "minimax" and provs["minimax"].cache_field == "cached_tokens"
assert provs["qwen"].reasoning == "enable_thinking"
assert provs["stepfun"].reasoning == "openai" and provs["stepfun"].efforts == ("low", "medium", "high")
assert provs["mimo"].reasoning == "thinking" and provs["mimo"].efforts == (), "mimo: no effort field"
print("shapes: thinking/effort/enable_thinking/minimax/openai + tiers + cache field per provider  ✓")

# ── specs: limits, vision, price, roles per MODEL ────────────────────────────
flash = ds.spec("deepseek-flash")
assert flash.context == 1_048_576 and flash.max_output == 384_000 and flash.vision
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
p, e, m = P.resolve("m3")  # unique bare model id
assert e.eid == "minimax" and m == "minimax-m3", e.eid
p, e, m = P.resolve("qwen-plan:max")
assert e.eid == "qwen-plan" and m == "qwen3.8-max"
# legacy DeepSeek ids (retired 2026-09-10) keep resolving — DeepSeek serves
# them with V4.1-Flash, and so does rocky's registry
assert P.resolve("deepseek-v4-flash")[2] == "deepseek-flash"
assert P.resolve("deepseek-v4-flash-vision-exp")[2] == "deepseek-flash"
assert P.resolve("deepseek:vision-exp")[2] == "deepseek-flash", "alias substring too"
assert P.resolve("deepseek:pro")[2] == "deepseek-v4-pro"
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
assert build_extra_body(True, "max", "thinking", efforts=()) == {"thinking": {"type": "enabled"}}, "mimo: no effort field"
print("effort: six wire shapes, thinking-off per shape, tiers clamped by position  ✓")

# ── per-provider key: rocky-owned only, never ambient; aliases honored ──────
for v in ("ROCKYCODE_MINIMAX_API_KEY", "ROCKYCODE_MINIMAX_CN_API_KEY", "MINIMAX_API_KEY"):
    os.environ.pop(v, None)
os.environ["MINIMAX_API_KEY"] = "sk-users-own-minimax"  # ambient — must be ignored
try:
    onboarding.provider_key("ROCKYCODE_MINIMAX_API_KEY", ("ROCKYCODE_MINIMAX_CN_API_KEY",))
    raise AssertionError("must not fall back to the ambient MINIMAX_API_KEY")
except RuntimeError as e:
    assert "ROCKYCODE_MINIMAX_API_KEY" in str(e)
os.environ["ROCKYCODE_MINIMAX_CN_API_KEY"] = "sk-rocky-minimax-old-name"
assert onboarding.provider_key("ROCKYCODE_MINIMAX_API_KEY", ("ROCKYCODE_MINIMAX_CN_API_KEY",)) \
    == "sk-rocky-minimax-old-name", "alias name still read"
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
assert pinned.context_window == 200_000 and pinned.max_tokens == 384_000
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
print("fee: peak is a per-provider schedule (deepseek only); unpriced models flag unset  ✓")

print("PROVIDERS SMOKE OK — models are data, the SDK is the only glue. amaze!")
