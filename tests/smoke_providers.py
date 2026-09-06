"""Provider profiles: multi-provider switching as DATA, no adapter glue.

Covers the declarative registry, `/model` spec resolution (provider+model),
the per-reasoning-policy extra_body shapes, rocky-owned per-provider keys,
and the engine's live provider switch. No network, no real client.
"""
import os
import tempfile
import types
from pathlib import Path

from rockycode.engine import providers as P
from rockycode.engine.effort import build_extra_body
from rockycode.engine.loop import Engine
from rockycode import onboarding

# ── registry: built-ins present, deepseek default mapping, EN/CN endpoints ───
provs = P.discover()
assert "deepseek" in provs and "minimax" in provs and "kimi" in provs, list(provs)
ds = provs["deepseek"]
assert ds.endpoints[0].key_env == onboarding.KEY_ENV, "deepseek → existing ROCKYCODE_API_KEY"
# explicit EN/CN ids + keys — no confusing bare default
eids = {e.eid: e for pr in provs.values() for e in pr.endpoints}
assert eids["kimi-en"].key_env == "ROCKYCODE_KIMI_EN_API_KEY"
assert eids["kimi-cn"].key_env == "ROCKYCODE_KIMI_CN_API_KEY"
assert eids["minimax-en"].key_env == "ROCKYCODE_MINIMAX_EN_API_KEY"
# GLM international arm is z.ai, a different brand id + key
assert "zai" in eids and eids["zai"].key_env == "ROCKYCODE_ZAI_EN_API_KEY"
assert "glm-cn" in eids and eids["zai"].base_url != eids["glm-cn"].base_url
print("registry: explicit kimi-en/kimi-cn/minimax-en/-cn keys + zai(intl)/glm-cn  ✓")

# ── resolve: provider, provider-region, provider:model, bare unique ──────────
p, e, m = P.resolve("deepseek:flash")
assert e.eid == "deepseek" and m == "deepseek-v4-flash", (e.eid, m)
p, e, m = P.resolve("kimi-cn")  # explicit region endpoint
assert e.eid == "kimi-cn" and m == "kimi-k3", (e.eid, m)
p, e, m = P.resolve("zai:glm")  # z.ai brand + model substring
assert e.eid == "zai" and m == "glm-5.2", (e.eid, m)
p, e, m = P.resolve("m3")  # unique bare model id
assert e.eid == "minimax-en" and m == "minimax-m3", e.eid
assert P.resolve("nope") is None
assert P.resolve("kimi-xx") is None, "unknown endpoint id → None"
assert P.resolve("zai:nope") is None, "unknown model substring → None"
print("resolve: endpoint-id / endpoint:model / bare-unique / unknown→None  ✓")

# ── configured picker stays SHORT: only keyed choices (+ deepseek always) ─────
for v in list(os.environ):
    if v.startswith("ROCKYCODE_") and v.endswith("_API_KEY") and v != onboarding.KEY_ENV:
        os.environ.pop(v, None)
short = P.configured_choices()
assert all(c.provider.name == "deepseek" for c in short), \
    "with no provider keys, the picker shows only deepseek"
os.environ["ROCKYCODE_KIMI_CN_API_KEY"] = "sk-rocky-kimi-cn"
short = P.configured_choices()
ids = {c.id for c in short}
assert "kimi-cn:kimi-k3" in ids, ids
assert not any(c.prov_id == "minimax-en" for c in short), "unkeyed endpoints stay hidden"
assert len(short) < len(P.choices()), "configured list is shorter than the full catalog"
os.environ.pop("ROCKYCODE_KIMI_CN_API_KEY", None)
print("picker: configured-only (short) vs full catalog; keying an endpoint reveals it  ✓")

# ── effort: the reasoning-param SHAPE is per provider policy, not per adapter ─
assert build_extra_body(True, "max", "deepseek") == {
    "thinking": {"type": "enabled"}, "reasoning_effort": "max"}
assert build_extra_body(True, "xhigh", "openai") == {"reasoning_effort": "high"}, \
    "openai policy: bare reasoning_effort, dial clamped"
assert build_extra_body(True, "max", "none") == {}, "none policy: no reasoning fields"
assert build_extra_body(False, "max", "deepseek") == {"thinking": {"type": "disabled"}}
assert build_extra_body(False, "max", "openai") == {}
print("effort: deepseek/openai/none param shapes, thinking off per policy  ✓")

# ── per-provider key: rocky-owned only, never ambient ────────────────────────
for v in ("ROCKYCODE_MINIMAX_API_KEY", "MINIMAX_API_KEY"):
    os.environ.pop(v, None)
os.environ["MINIMAX_API_KEY"] = "sk-users-own-minimax"  # ambient — must be ignored
try:
    onboarding.provider_key("ROCKYCODE_MINIMAX_API_KEY")
    raise AssertionError("must not fall back to the ambient MINIMAX_API_KEY")
except RuntimeError as e:
    assert "ROCKYCODE_MINIMAX_API_KEY" in str(e)
os.environ["ROCKYCODE_MINIMAX_API_KEY"] = "sk-rocky-minimax"
assert onboarding.provider_key("ROCKYCODE_MINIMAX_API_KEY") == "sk-rocky-minimax"
os.environ.pop("ROCKYCODE_MINIMAX_API_KEY", None); os.environ.pop("MINIMAX_API_KEY", None)
print("key: rocky-owned ROCKYCODE_<P>_API_KEY only, ambient provider key ignored  ✓")

# ── engine: live provider switch swaps client+model+policy, not history ──────
client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace()))
eng = Engine(model="deepseek-v4-pro", client=client, workdir=Path(tempfile.mkdtemp()),
             system_prompt="BASE")
assert eng.reasoning_policy == "deepseek" and eng.provider_name == "deepseek"
assert eng._extra_body() == {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}
before = list(eng.history)
new_client = types.SimpleNamespace(chat="minimax-client")
eng.switch_provider(new_client, "minimax-m3", provider_name="minimax-cn", reasoning_policy="openai")
assert eng.client is new_client and eng.model == "minimax-m3"
assert eng.provider_name == "minimax-cn" and eng.reasoning_policy == "openai"
assert eng._extra_body() == {"reasoning_effort": "high"}, eng._extra_body()
assert eng.history == before, "a provider switch must not touch conversation history"
print("engine: switch swaps client+model+policy, history untouched  ✓")

# ── model-level vision: one deepseek provider, only vision-exp sees ──────────
ds = P.discover()["deepseek"]
assert "deepseek-v4-flash-vision-exp" in ds.models and not ds.vision
by_model = {c.model: c for c in P.choices() if c.provider.name == "deepseek"}
assert by_model["deepseek-v4-flash-vision-exp"].vision, "vision-exp sees images"
assert not by_model["deepseek-v4-flash"].vision and not by_model["deepseek-v4-pro"].vision
p, e, m = P.resolve("deepseek:flash")
assert m == "deepseek-v4-flash", "base model wins its own substring vs -vision-exp"
p, e, m = P.resolve("deepseek:vision")
assert m == "deepseek-v4-flash-vision-exp", "the variant stays reachable by 'vision'"
assert P.resolve("deepseek-v4-flash-vision-exp")[2] == "deepseek-v4-flash-vision-exp"
print("vision: per-MODEL flag — vision-exp ❖, flash/pro text; resolve stays sharp  ✓")

# ── config vision_models: "flash gained vision" is a config flip, not code ───
from rockycode import config as C
C.GLOBAL_PATH = Path(tempfile.mkdtemp()) / "config.toml"  # never the real ~/.rockycode
C.set_value("vision_models", "deepseek-v4-flash, no-such-model")
flip = {c.model: c for c in P.choices() if c.provider.name == "deepseek"}
assert flip["deepseek-v4-flash"].vision, "config vision_models marks flash as seeing"
assert "no-such-model" not in P.discover()["deepseek"].vision_models, "unknown ids ignored"
C.set_value("vision_models", "")
assert not [c for c in P.choices() if c.provider.name == "deepseek" and
            c.model == "deepseek-v4-flash"][0].vision, "cleared → flash text-only again"
print("config: vision_models flips a model's sight from any shell — no code change  ✓")

# ── endpoints.toml: an own base URL becomes a first-class <provider>-custom ──
eid, err = P.set_custom_url("kimi", "https://my-gateway.example/v1")
assert err is None and eid == "kimi-custom"
kimi = P.discover()["kimi"]
custom = [e for e in kimi.endpoints if e.eid == "kimi-custom"]
assert custom and custom[0].base_url == "https://my-gateway.example/v1"
assert custom[0].key_env == kimi.endpoints[0].key_env, "custom URL rides the provider's key"
p, e, m = P.resolve("kimi-custom:kimi-k3")
assert e.eid == "kimi-custom" and m == "kimi-k3", "custom endpoint is /model-addressable"
_eid, err = P.set_custom_url("kimi", "ftp://nope")
assert err is not None, "non-http url refused"
_eid, err = P.set_custom_url("kimi", "")  # empty removes the entry
assert err is None and not [e for e in P.discover()["kimi"].endpoints if e.eid == "kimi-custom"]
print("endpoints.toml: own gateway URL per provider — added, resolvable, removable  ✓")

print("PROVIDERS SMOKE OK — providers are data, the SDK is the only glue. amaze!")


# ── fee alignment: peak is DeepSeek-only, cross-provider models price honest ──
from datetime import datetime, timezone
from rockycode.pricing import UsageLedger

led = UsageLedger()
# an unpriced provider model → 0 cost + flagged unconfigured (NOT DeepSeek's $)
assert not led.priced("minimax-m3"), "provider model has no built-in rate"
led.add("minimax-m3", {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000})
assert led.cost("usd") == 0.0, "unpriced model must not read as DeepSeek dollars"
assert not led.configured("usd"), "unpriced usage flags '(prices unset)'"
# peak surcharge applies to DeepSeek only — mid-peak-window timestamp
peak_at = datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc)  # inside 01:00–04:00, after effective date
ds = UsageLedger(); ds.add("deepseek-v4-flash", {"prompt_tokens": 1_000_000}, at=peak_at)
mm = UsageLedger(); mm.add("minimax-m3", {"prompt_tokens": 1_000_000}, at=peak_at)
assert any(peak for (_m, peak) in ds.buckets), "deepseek turn is peak-bucketed in-window"
assert not any(peak for (_m, peak) in mm.buckets), "minimax turn is NEVER peak-multiplied"
print("fee: peak is deepseek-only; unpriced provider models flag unset (not $DeepSeek)  ✓")
