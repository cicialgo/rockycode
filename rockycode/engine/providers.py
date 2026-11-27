"""Provider + model registry: catalog metadata, not protocol certification.

The current providers use OpenAI-compatible Chat Completions. The SDK shares
transport machinery; it does not guarantee identical model semantics. Limits,
prices, roles, wire IDs and each model's request contract (the switches in
engine/model_protocol.py) live in `rockycode/models.toml`, merged with the
user's override — a new model with a known behaviour is one row. A new
reasoning wire shape or a behaviour no model has had yet still needs code
and conformance tests; a row alone cannot provide those.
See docs/design/model-integration-roadmap.md for the per-model audit and plan.

Endpoints: one cn base_url per provider (no en/cn split anymore), plus an
optional subscription-plan endpoint (`<provider>-plan`, own URL + own key),
plus an own-URL row from ~/.rockycode/endpoints.toml (`<provider>-custom`).
OpenRouter is the builtin exception: an international free host
(`https://openrouter.ai/api/v1`), keyed by `ROCKYCODE_OPENROUTER_API_KEY`,
reasoning shape `none`, the generic contract. Keys are rocky-OWNED per
endpoint — `ROCKYCODE_<NAME>_API_KEY` — never the ambient `MINIMAX_API_KEY`
or `OPENROUTER_API_KEY`; the older regional names (`_CN_`/`_EN_`) are still
read as aliases. The builtin `deepseek` reads ROCKYCODE_API_KEY.

Local servers (Ollama) are discovered live, not catalogued — see local_models.
Custom OpenAI-compatible servers still come from ~/.rockycode/providers.toml.
"""
from __future__ import annotations

import os
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rockycode.engine.model_protocol import GENERIC, SWITCHES, ModelProtocol
from rockycode.onboarding import BASE_URL_ENV, DEFAULT_BASE_URL, KEY_ENV

# Fallbacks for a model the registry does NOT know (a custom providers.toml
# server without limits): the same generous numbers rocky always assumed, so
# nothing changes for existing setups; a known model brings its own.
DEFAULT_CONTEXT = 1_048_576
DEFAULT_MAX_OUTPUT = 384_000
# rocky-side context_window default for LOCAL providers. Ollama's serve ctx is
# small and it truncates SILENTLY, so a 1M default would mean compaction never
# fires in time — 64k matches the serve-side setting the docs recommend
# (OLLAMA_CONTEXT_LENGTH=65536). The /model preflight replaces this with the
# VERIFIED effective ctx whenever the server reports one.
LOCAL_CONTEXT_WINDOW = 65_536
# Same rocky-owned env the memory index / dream pipeline already read — one
# name for "where is my ollama", never the ambient OLLAMA_HOST.
OLLAMA_URL_ENV = "ROCKYCODE_OLLAMA_URL"
_OLLAMA_DEFAULT_ROOT = "http://localhost:11434"

_HOME = Path(os.environ.get("ROCKYCODE_HOME") or Path.home() / ".rockycode")
# The shipped catalog and the user's override — same shape, deep-merged.
BUILTIN_MODELS_TOML = Path(__file__).resolve().parents[1] / "models.toml"
MODELS_TOML = _HOME / "models.toml"
# Custom OpenAI-compatible servers (hand-maintained, older shape, still honored).
PROVIDERS_TOML = _HOME / "providers.toml"
# Own-URL overrides (proxies, self-hosted gateways): a flat, rocky-OWNED file —
# `<provider> = "<base_url>"` — that discover() turns into an extra
# `<provider>-custom` endpoint on the builtin. Separate from providers.toml so
# rocky can rewrite it safely (the picker's "custom base URL" row writes here)
# without ever touching a hand-maintained file.
ENDPOINTS_TOML = _HOME / "endpoints.toml"
_PLACEHOLDERS = {"", "replace-me", "sk-replace-me", "your-api-key"}

# Reasoning wire shapes a provider can declare (effort.build_extra_body):
#   thinking        {thinking: {type}} + reasoning_effort   (deepseek, glm, mimo)
#   effort          reasoning_effort only, always on         (kimi)
#   enable_thinking {enable_thinking: bool}                   (qwen / DashScope)
#   minimax         {thinking: {type: adaptive|disabled}, reasoning_split}
#   openai          bare reasoning_effort low|medium|high     (stepfun, generic)
#   none            nothing on the wire                       (ollama, strict servers)
# "deepseek" is accepted as a legacy spelling of "thinking".
REASONING_SHAPES = ("thinking", "effort", "enable_thinking", "minimax", "openai", "none")


@dataclass
class Endpoint:
    # `eid` is the EXPLICIT /model token — `qwen`, `qwen-plan`, `kimi-custom`.
    # `key_env` is the rocky-owned var; `key_aliases` are older names still
    # honored (the pre-cn-only `_CN_`/`_EN_` spellings) so no setup breaks.
    eid: str
    base_url: str
    key_env: str  # "" = keyless (a LOCAL server) — always configured
    key_aliases: tuple[str, ...] = ()
    label: str = ""  # "Bailian Token Plan" on a plan row; "" = the official endpoint

    @property
    def keyless(self) -> bool:
        return not self.key_env

    def key(self) -> Optional[str]:
        if self.keyless:
            # Local servers take no auth, but the OpenAI SDK requires a
            # non-empty api_key string — this placeholder never leaves the box.
            return "local"
        for name in (self.key_env, *self.key_aliases):
            v = (os.getenv(name) or "").strip()
            if v and v.lower() not in _PLACEHOLDERS:
                return v
        return None

    def key_source(self) -> Optional[str]:
        """Which env var actually supplied the key (the alias, if one did) —
        for messages, never the value."""
        if self.keyless:
            return None
        for name in (self.key_env, *self.key_aliases):
            v = (os.getenv(name) or "").strip()
            if v and v.lower() not in _PLACEHOLDERS:
                return name
        return None


@dataclass
class ModelSpec:
    """What the engine needs to know about ONE model, as data."""
    id: str
    provider: str
    label: str = ""
    context: int = DEFAULT_CONTEXT
    max_output: int = DEFAULT_MAX_OUTPUT
    vision: bool = False
    price: dict = field(default_factory=dict)  # {"usd": {in_hit,in_miss,out}, "cny": …}
    roles: tuple[str, ...] = ()
    note: str = ""
    # API model id when the provider's docs spell it differently from `id`
    # (the /model token). Empty = send `id`.
    wire: str = ""
    # The request contract the row's switches ask for (history, sampling,
    # completion — engine/model_protocol.py). No switches = the generic one.
    protocol: ModelProtocol = GENERIC
    # None = inherit the provider. A tuple overrides (including () = no effort).
    efforts: Optional[tuple[str, ...]] = None
    # None = inherit the provider. False = this model 400s if thinking is disabled.
    thinking_off: Optional[bool] = None
    # Empty = available on all endpoints of this provider.
    endpoint_ids: tuple[str, ...] = ()


@dataclass
class Provider:
    name: str
    models: list[str]
    endpoints: list[Endpoint]
    reasoning: str = "openai"   # a REASONING_SHAPES entry
    tools: str = "native"      # native | off
    # vision=True: EVERY listed model takes image input. Builtins mark vision
    # per MODEL (ModelSpec.vision / vision_models) — capability is per-CHOICE:
    # Choice.vision is the one question callers ask; config key `vision_models`
    # adds names here at discover() time, so "a model gained vision" is a
    # config flip, not a code change.
    vision: bool = False
    vision_models: list[str] = field(default_factory=list)
    label: str = ""
    builtin: bool = True
    # local=True: models run on the user's own machine — keyless endpoints,
    # $0 pricing in the footer, a smaller context_window default, and the
    # /model switch runs a readiness preflight before pointing the engine at it.
    local: bool = False
    # The provider's own effort tiers, low→high. None = the wire shape's
    # default list; () = send no effort field at all.
    efforts: Optional[tuple[str, ...]] = None
    thinking_off: bool = True   # False: thinking can't be disabled (glm, kimi)
    cache_field: str = "cached_tokens"  # prompt_cache_hit_tokens | cached_tokens
    search: str = ""            # provider-side web search backend ("anthropic")
    peak: str = ""              # [peak.<name>] surcharge schedule, if any
    aliases: dict[str, str] = field(default_factory=dict)  # old id → current id
    specs: dict[str, ModelSpec] = field(default_factory=dict)

    def spec(self, model: str) -> ModelSpec:
        """The registry entry for *model*, or a default-shaped one (a custom
        server's model, a live-discovered local model)."""
        s = self.specs.get(model)
        if s is not None:
            return s
        return ModelSpec(
            id=model, provider=self.name,
            context=LOCAL_CONTEXT_WINDOW if self.local else DEFAULT_CONTEXT,
            vision=self.vision or model in self.vision_models,
        )

    def api_model(self, model: str) -> str:
        """The id that goes on the wire. The registry key stays the /model token."""
        spec = self.specs.get(model)
        return spec.wire if spec is not None and spec.wire else model

    def efforts_for(self, model: str) -> Optional[tuple[str, ...]]:
        spec = self.specs.get(model)
        if spec is not None and spec.efforts is not None:
            return spec.efforts
        return self.efforts

    def thinking_off_for(self, model: str) -> bool:
        spec = self.specs.get(model)
        if spec is not None and spec.thinking_off is not None:
            return spec.thinking_off
        return self.thinking_off

    def endpoints_for(self, model: str) -> list[Endpoint]:
        ids = self.spec(model).endpoint_ids
        return [endpoint for endpoint in self.endpoints if not ids or endpoint.eid in ids]


@dataclass
class Choice:
    """A flat, pickable (provider, endpoint, model). `id` is `<eid>:<model>`
    (e.g. kimi:kimi-k3 / qwen-plan:qwen3.8-max); `configured` = its key is
    set, which is how the picker stays short (show only what you've keyed)."""
    provider: Provider
    endpoint: Endpoint
    model: str

    @property
    def prov_id(self) -> str:
        return self.endpoint.eid

    @property
    def id(self) -> str:
        return f"{self.endpoint.eid}:{self.model}"

    @property
    def configured(self) -> bool:
        return self.endpoint.key() is not None

    @property
    def spec(self) -> ModelSpec:
        return self.provider.spec(self.model)

    @property
    def vision(self) -> bool:
        """Does THIS model take image input? Model-level wins; a provider-level
        vision=True still means all of them."""
        return (self.provider.vision or self.model in self.provider.vision_models
                or self.spec.vision)


def ollama_root() -> str:
    """The local Ollama server's ROOT url (no /v1) — rocky-owned env override
    for tests/relocated servers, same var the memory index reads."""
    return (os.getenv(OLLAMA_URL_ENV) or _OLLAMA_DEFAULT_ROOT).strip().rstrip("/")


# Live-discovery cache for local model listings: base_url → (at, models, up).
# A hardcoded list is wrong by design (what's pulled is per-machine), but
# discover() runs on hot paths (picker open, resolve), so the probe is cached —
# briefly on failure so `ollama serve` then retry works without a restart.
_local_cache: dict[str, tuple[float, list[str], bool]] = {}
_LOCAL_TTL_UP, _LOCAL_TTL_DOWN = 30.0, 5.0


def local_models(base_url: str) -> tuple[list[str], bool]:
    """(pulled model ids, server up?) from a local server's GET /v1/models.
    Never raises; a down server is a normal state, not an error. The timeout is
    short — localhost refusal returns instantly; only a hung server waits."""
    now = time.monotonic()
    hit = _local_cache.get(base_url)
    if hit and now - hit[0] < (_LOCAL_TTL_UP if hit[2] else _LOCAL_TTL_DOWN):
        return list(hit[1]), hit[2]
    models: list[str] = []
    up = False
    try:
        import json
        import urllib.request
        with urllib.request.urlopen(base_url.rstrip("/") + "/models", timeout=1.5) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        models = [str(m["id"]) for m in (data.get("data") or [])
                  if isinstance(m, dict) and m.get("id")]
        up = True
    except Exception:  # noqa: BLE001 — unreachable/odd reply = not running
        models, up = [], False
    _local_cache[base_url] = (now, models, up)
    return list(models), up


# ── the registry file(s) ─────────────────────────────────────────────────────

def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 — a broken file must not crash startup
        return {}


def _deep_merge(base: dict, over: dict) -> None:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def registry_data() -> dict:
    """The shipped catalog with the user's ~/.rockycode/models.toml merged on
    top (raw dicts — pricing reads the peak schedules from here too)."""
    data = _read_toml(BUILTIN_MODELS_TOML)
    _deep_merge(data, _read_toml(MODELS_TOML))
    return data


def _key_env_for(name: str) -> str:
    return f"ROCKYCODE_{name.upper().replace('-', '_')}_API_KEY"


def _slug(name: str) -> str:
    """A provider name as add_provider stores it."""
    return re.sub(r"[^a-z0-9_-]", "", name.strip().lower())


def key_env_for(name: str) -> str:
    """The rocky-owned variable a custom provider named *name* reads its key from."""
    return _key_env_for(_slug(name))


def catalog_owner(name: str) -> str:
    """The catalog provider a custom server named *name* would replace or share
    a key with, or "". add_provider replaces a provider of the same name and a
    provider finds its key by its name, so reusing a catalog name (or one of
    its endpoint ids) would send that provider's saved key to the new address."""
    slug = _slug(name)
    env = _key_env_for(slug)
    for prov in _providers_from_data(_read_toml(BUILTIN_MODELS_TOML)).values():
        for ep in prov.endpoints:
            if slug in (prov.name, ep.eid) or env in (ep.key_env, *ep.key_aliases):
                return prov.name
    return ""


# Every field a model row may carry (the header of models.toml explains each).
# A row with any other field is refused: a misspelt switch would otherwise run
# the generic contract without a word.
MODEL_FIELDS = frozenset({
    "provider", "label", "context", "max_output", "vision", "price", "roles", "note",
    "wire", "efforts", "thinking_off", "endpoint_ids", "hidden",
    "served_model", "verified_at", "sources", *SWITCHES})


def _model_spec(mid: str, m: dict, provider: str) -> ModelSpec:
    """One catalog row as a ModelSpec. A ValueError says what is wrong with it."""
    unknown = sorted(set(m) - MODEL_FIELDS)
    if "protocol" in unknown:
        raise ValueError("`protocol` is gone — write the model's switches in its row instead "
                         f"({', '.join(SWITCHES)})")
    if unknown:
        import difflib
        near = difflib.get_close_matches(unknown[0], sorted(MODEL_FIELDS), n=1)
        raise ValueError(f"unknown field `{unknown[0]}`" + (f" — did you mean `{near[0]}`?" if near else ""))
    protocol = ModelProtocol.from_row(m)
    try:
        return ModelSpec(
            id=mid, provider=provider, label=str(m.get("label") or ""),
            context=int(m.get("context") or DEFAULT_CONTEXT),
            max_output=int(m.get("max_output") or DEFAULT_MAX_OUTPUT),
            vision=bool(m.get("vision", False)),
            price={k: dict(v) for k, v in (m.get("price") or {}).items()
                   if isinstance(v, dict)},
            roles=tuple(str(r) for r in (m.get("roles") or ())),
            note=str(m.get("note") or ""),
            wire=str(m.get("wire") or ""),
            protocol=protocol,
            efforts=(tuple(str(e) for e in m["efforts"]) if "efforts" in m else None),
            thinking_off=(bool(m["thinking_off"]) if "thinking_off" in m else None),
            endpoint_ids=tuple(str(e) for e in (m.get("endpoint_ids") or ())),
        )
    except (TypeError, ValueError, AttributeError):
        raise ValueError("a field has the wrong type (context and max_output are whole numbers; "
                         "roles, efforts and endpoint_ids are lists; price is a table)") from None


def _providers_from_data(data: dict) -> dict[str, Provider]:
    provs: dict[str, Provider] = {}
    for name, cfg in (data.get("providers") or {}).items():
        if not isinstance(cfg, dict) or cfg.get("hidden"):
            continue
        base = str(cfg.get("base_url") or "").strip()
        if not base:
            continue
        key_env = str(cfg.get("key_env") or _key_env_for(name))
        if name == "deepseek":
            # The home provider keeps the env transport: ROCKYCODE_BASE_URL
            # (a proxy, a gateway) overrides the catalog URL, same as always.
            base = (os.getenv(BASE_URL_ENV) or "").strip() or base or DEFAULT_BASE_URL
            key_env = KEY_ENV
        eps = [Endpoint(name, base, key_env,
                        tuple(str(a) for a in (cfg.get("key_aliases") or [])))]
        plan = cfg.get("plan")
        if isinstance(plan, dict) and plan.get("base_url"):
            eps.append(Endpoint(
                f"{name}-plan", str(plan["base_url"]).strip(),
                str(plan.get("key_env") or f"ROCKYCODE_{name.upper()}_PLAN_API_KEY"),
                label=str(plan.get("label") or "plan")))
        reasoning = str(cfg.get("reasoning") or "openai")
        provs[name] = Provider(
            name=name, models=[], endpoints=eps,
            reasoning="thinking" if reasoning == "deepseek" else reasoning,
            tools=str(cfg.get("tools") or "native"),
            label=str(cfg.get("label") or name),
            local=bool(cfg.get("local", False)),
            efforts=(tuple(str(e) for e in cfg["efforts"]) if "efforts" in cfg else None),
            thinking_off=bool(cfg.get("thinking_off", True)),
            cache_field=str(cfg.get("cache_field") or "cached_tokens"),
            search=str(cfg.get("search") or ""),
            peak=str(cfg.get("peak") or ""),
            aliases={str(k): str(v) for k, v in (cfg.get("aliases") or {}).items()},
        )
    for mid, m in (data.get("models") or {}).items():
        if not isinstance(m, dict) or m.get("hidden"):
            continue
        p = provs.get(str(m.get("provider") or ""))
        if p is None:
            continue
        try:
            spec = _model_spec(str(mid), m, p.name)
        except ValueError:
            continue  # never fatal — refused_rows() says why, wherever the model is asked for
        p.specs[spec.id] = spec
        p.models.append(spec.id)
        if spec.vision and spec.id not in p.vision_models:
            p.vision_models.append(spec.id)
    return {n: p for n, p in provs.items() if p.models}


def catalog() -> dict[str, Provider]:
    """The cloud providers from the registry file(s) only — no local probe, no
    legacy providers.toml. Pricing reads its tables from here."""
    return _providers_from_data(registry_data())


def _builtins() -> dict[str, Provider]:
    provs = catalog()
    # LOCAL: whatever Ollama has pulled on THIS machine, discovered live —
    # the picker shows reality, not a catalog. Keyless, $0, reasoning
    # "none" (the compat layer takes no reasoning params). Not running →
    # zero models; the picker shows a "start it" hint instead (see
    # local_hints) and /model gives the exact fix.
    provs["ollama"] = Provider(
        name="ollama",
        models=local_models(ollama_root() + "/v1")[0],
        endpoints=[Endpoint("ollama", ollama_root() + "/v1", "")],
        reasoning="none", local=True,
        label="Ollama — local · $0")
    return provs


def _parse_toml(path: Path) -> dict[str, Provider]:
    """~/.rockycode/providers.toml — custom OpenAI-compatible servers in the
    older per-provider shape (models list, base_url or endpoints, vision,
    reasoning, local). Optional `context` / `max_output` set every listed
    model's limits; anything richer belongs in ~/.rockycode/models.toml."""
    data = _read_toml(path)
    out: dict[str, Provider] = {}
    for name, cfg in (data.get("providers") or data).items():
        if not isinstance(cfg, dict):
            continue
        models = cfg.get("models") or ([cfg["model"]] if cfg.get("model") else [])
        # endpoints: explicit list, or a single base_url/key_env pair. Each
        # endpoint's `id` is its /model token; key_env defaults to
        # ROCKYCODE_<ID_UPPER>_API_KEY — except `local = true` providers
        # (LM Studio, llama.cpp, another box's ollama), whose endpoints
        # default to keyless. An explicit key_env always wins.
        local = bool(cfg.get("local", False))

        def _kenv(eid: str) -> str:
            return "" if local else _key_env_for(eid)
        eps_cfg = cfg.get("endpoints")
        if eps_cfg:
            eps = [Endpoint(e.get("id", name), e["base_url"],
                            e.get("key_env", _kenv(e.get("id", name))))
                   for e in eps_cfg if e.get("base_url")]
        elif cfg.get("base_url"):
            eps = [Endpoint(cfg.get("id", name), cfg["base_url"],
                            cfg.get("key_env", _kenv(cfg.get("id", name))))]
        else:
            continue
        if not models or not eps:
            continue
        reasoning = str(cfg.get("reasoning", "openai"))
        p = Provider(name=name, models=[str(m) for m in models], endpoints=eps,
                     reasoning="thinking" if reasoning == "deepseek" else reasoning,
                     tools=cfg.get("tools", "native"),
                     vision=bool(cfg.get("vision", False)),
                     vision_models=[str(m) for m in cfg.get("vision_models") or []],
                     label=cfg.get("label", ""), builtin=False,
                     local=local, thinking_off=bool(cfg.get("thinking_off", True)),
                     cache_field=str(cfg.get("cache_field") or "cached_tokens"))
        if cfg.get("context") or cfg.get("max_output"):
            for m in p.models:
                try:
                    p.specs[m] = ModelSpec(
                        id=m, provider=name,
                        context=int(cfg.get("context") or (LOCAL_CONTEXT_WINDOW if local else DEFAULT_CONTEXT)),
                        max_output=int(cfg.get("max_output") or DEFAULT_MAX_OUTPUT),
                        vision=p.vision or m in p.vision_models)
                except (TypeError, ValueError):
                    pass
        out[name] = p
    return out


def _custom_urls() -> dict[str, str]:
    """~/.rockycode/endpoints.toml: flat `<provider> = "<base_url>"` lines."""
    data = _read_toml(ENDPOINTS_TOML)
    return {k: v.strip() for k, v in data.items()
            if isinstance(v, str) and v.strip().startswith(("http://", "https://"))}


def set_custom_url(provider_name: str, base_url: str) -> tuple[Optional[str], Optional[str]]:
    """Persist an own base URL for *provider_name* (the picker's "custom base
    URL" row). Returns (endpoint id, error). An empty url removes the entry.
    The file is flat and rocky-owned, so a full rewrite is safe."""
    base_url = base_url.strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        return None, "base URL must start with http:// or https://"
    urls = _custom_urls()
    if base_url:
        urls[provider_name] = base_url
    else:
        urls.pop(provider_name, None)
    try:
        ENDPOINTS_TOML.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(f'{k} = "{v}"\n' for k, v in sorted(urls.items()))
        ENDPOINTS_TOML.write_text("# own base URLs per provider — written by the "
                                  "/model picker (custom base URL row)\n" + body)
    except OSError as e:
        return None, f"could not write {ENDPOINTS_TOML}: {e}"
    return (f"{provider_name}-custom" if base_url else None), None


def _toml_str(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def add_provider(name: str, base_url: str, models: list[str], *,
                 key_env: str = "", reasoning: str = "openai", vision: bool = False,
                 context: int = 0, max_output: int = 0,
                 label: str = "") -> tuple[Optional[str], Optional[str]]:
    """Add (or replace) a custom OpenAI-compatible provider in the rocky-OWNED
    override ~/.rockycode/models.toml — the `rocky_config` tool's path, so
    "add my vLLM server" is something rocky can do for the user. Returns
    (provider name, error). Never writes a key: only its env var NAME."""
    name = _slug(name)
    if not name:
        return None, "provider name must be letters/digits/-/_"
    base_url = base_url.strip()
    if not base_url.startswith(("http://", "https://")):
        return None, "base URL must start with http:// or https://"
    models = [m.strip() for m in models if m and m.strip()]
    if not models:
        return None, "at least one model id is required"
    if reasoning not in REASONING_SHAPES:
        return None, f"reasoning must be one of {', '.join(REASONING_SHAPES)}"
    data = _read_toml(MODELS_TOML)
    provs = data.setdefault("providers", {})
    entry: dict = {"label": label or name, "base_url": base_url,
                   "key_env": key_env.strip() or _key_env_for(name),
                   "reasoning": reasoning}
    provs[name] = entry
    mods = data.setdefault("models", {})
    # drop this provider's previous models so a replace doesn't leave strays
    for mid in [k for k, v in mods.items() if isinstance(v, dict) and v.get("provider") == name]:
        mods.pop(mid, None)
    for m in models:
        row: dict = {"provider": name, "vision": bool(vision)}
        if context > 0:
            row["context"] = int(context)
        if max_output > 0:
            row["max_output"] = int(max_output)
        mods[m] = row
    try:
        MODELS_TOML.parent.mkdir(parents=True, exist_ok=True)
        MODELS_TOML.write_text(_emit_models_toml(data))
    except OSError as e:
        return None, f"could not write {MODELS_TOML}: {e}"
    return name, None


def _emit_models_toml(data: dict) -> str:
    """Serialize the override file: only the shapes rocky itself writes
    (flat provider/model tables with scalar values + a price sub-table)."""
    def scalar(v) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, (list, tuple)):
            return "[" + ", ".join(scalar(x) for x in v) + "]"
        return _toml_str(v)
    lines = ["# rocky's own model registry override — merged over the builtin "
             "rockycode/models.toml\n# (written by rocky_config / add_provider; "
             "hand edits are fine, same shape)\n"]
    for section in ("providers", "models", "peak"):
        for key, tbl in (data.get(section) or {}).items():
            if not isinstance(tbl, dict):
                continue
            lines.append(f'[{section}.{_toml_str(key)}]')
            subs = {}
            for k, v in tbl.items():
                if isinstance(v, dict):
                    subs[k] = v
                else:
                    lines.append(f"{k} = {scalar(v)}")
            for k, sub in subs.items():
                lines.append(f'[{section}.{_toml_str(key)}.{k}]')
                for kk, vv in sub.items():
                    if isinstance(vv, dict):
                        inner = ", ".join(f"{a} = {scalar(b)}" for a, b in vv.items())
                        lines.append(f"{kk} = {{ {inner} }}")
                    else:
                        lines.append(f"{kk} = {scalar(vv)}")
            lines.append("")
    return "\n".join(lines) + "\n"


def _config_vision_models() -> list[str]:
    # Config key `vision_models`: extra model ids to treat as image-capable —
    # for when a provider ships vision on an EXISTING id before this registry
    # catches up. Scriptable from any shell (another rocky included):
    # `rockycode config vision_models <id>[,<id>…]`.
    from rockycode.config import load as _load_cfg
    raw = str(_load_cfg().get("vision_models") or "")
    return [m for m in raw.replace(",", " ").split() if m]


def discover() -> dict[str, Provider]:
    providers = _builtins()
    providers.update(_parse_toml(PROVIDERS_TOML))
    for name, url in _custom_urls().items():
        p = providers.get(name)
        # the custom endpoint rides the provider's PRIMARY key by default (a
        # proxy for provider X almost always takes provider X's key); a
        # different key wants a full providers.toml entry instead.
        if p is not None and not any(e.eid == f"{name}-custom" for e in p.endpoints):
            p.endpoints.append(Endpoint(f"{name}-custom", url, p.endpoints[0].key_env,
                                        p.endpoints[0].key_aliases, label="custom URL"))
    extra = _config_vision_models()
    if extra:
        for p in providers.values():
            p.vision_models = p.vision_models + [
                m for m in extra if m in p.models and m not in p.vision_models]
    return providers


def choices() -> list[Choice]:
    """Every (provider, endpoint, model) as a flat pickable list."""
    return [Choice(p, e, m) for p in discover().values()
            for e in p.endpoints for m in p.models if e in p.endpoints_for(m)]


def key_rows() -> list[tuple[Provider, Endpoint]]:
    """Every endpoint a key can be saved for, in catalog order. A local server
    takes no key; an endpoint that serves no model has no use for one."""
    return [(p, e) for p in discover().values() for e in p.endpoints
            if not e.keyless and any(e in p.endpoints_for(m) for m in p.models)]


def configured_choices() -> list[Choice]:
    """The SHORT list the picker shows by default, so a catalog of ~12
    doesn't scroll. A model is listed when one of its endpoints is keyed
    (deepseek always — it rides ROCKYCODE_API_KEY). Every endpoint of that
    model comes along: the plan row and a saved own-URL, keyed or not.
    Dropping the unkeyed siblings made the URL step vanish on the next bare
    /model — one configured endpoint, so the picker switched straight away
    and the base URL could no longer be picked or edited."""
    all_c = choices()
    shown = {(c.provider.name, c.model) for c in all_c
             if c.configured or c.provider.name == "deepseek"}
    return [c for c in all_c if (c.provider.name, c.model) in shown]


def role_choice(role: str) -> Optional[Choice]:
    """The first KEYED choice whose model declares *role* ("sidecar",
    "search", …) — official endpoint before plan/custom rows. None when no
    keyed model carries the role."""
    for c in choices():
        if role in c.spec.roles and c.configured:
            return c
    return None


def role_model(role: str) -> Optional[str]:
    """The model id for *role* regardless of keys (the search backend rides
    the home key anyway) — None if the catalog declares no such role."""
    for p in catalog().values():
        for m in p.models:
            if role in p.spec(m).roles:
                return m
    return None


def cache_hit_tokens(usage: dict, cache_field: str) -> Optional[int]:
    """Prompt-cache hit tokens from a usage dict in the provider's own shape:
    DeepSeek's top-level prompt_cache_hit_tokens, or the OpenAI-style
    prompt_tokens_details.cached_tokens. None when not reported."""
    if cache_field == "prompt_cache_hit_tokens":
        v = usage.get("prompt_cache_hit_tokens")
    else:
        d = usage.get("prompt_tokens_details")
        v = d.get("cached_tokens") if isinstance(d, dict) else None
    return int(v) if isinstance(v, int) and not isinstance(v, bool) else None


def split_spec(spec: str) -> tuple[str, str]:
    """A /model spec's (endpoint part, model part): split on the FIRST `:` or
    space ONLY — the model part keeps any further colons, because ollama tags
    contain one (`qwen3.5:2b`). A replace-all-then-split here once turned
    `ollama:qwen3.5:2b` into model "qwen3.5 2b", unmatchable forever."""
    parts = re.split(r"[:\s]", spec.strip(), maxsplit=1)
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")


def resolve(spec: str) -> Optional[tuple[Provider, Endpoint, str]]:
    """Resolve a /model arg to (provider, endpoint, model).

    Forms: an endpoint id (`deepseek`, `qwen-plan`, `kimi-custom`),
    `endpoint:model` (`kimi:k3`, `glm:flash`, `ollama:qwen3.5:2b`), or a
    unique bare model substring. Legacy ids listed under a provider's
    `aliases` resolve to their current model. Returns None if the endpoint is
    unknown or the model substring is ambiguous.
    """
    spec = spec.strip()
    eid_part, model_part = split_spec(spec)
    provs = discover()

    by_eid = {e.eid: (p, e) for p in provs.values() for e in p.endpoints}
    if eid_part in by_eid:
        p, e = by_eid[eid_part]
        models = [m for m in p.models if e in p.endpoints_for(m)]
        if not models:  # a local provider with nothing discovered — see local_hint()
            return None
        if not model_part:
            return p, e, models[0]
        part = _unalias(p, model_part)
        m = _pick_model(part, models)
        if m is None:
            # A plan-only addition must not steal an existing shorthand such as qwen-plan:max.
            m = _pick_model(part, [model for model in models if not p.spec(model).endpoint_ids])
        return (p, e, m) if m else None

    # a legacy id on its own → its provider's current model
    for prov in provs.values():
        if spec in prov.aliases:
            model = prov.aliases[spec]
            if model not in prov.models:  # its row is hidden or refused: no default-shaped stand-in
                return None
            endpoints = prov.endpoints_for(model)
            return (prov, endpoints[0], model) if endpoints else None
    # bare model — must pick uniquely across all providers (first endpoint)
    all_models = [m for prov in provs.values() for m in prov.models]
    m = _pick_model(spec, all_models)
    if m is None:
        return None
    for prov in provs.values():
        if m in prov.models:
            endpoints = prov.endpoints_for(m)
            return (prov, endpoints[0], m) if endpoints else None
    return None


def _unalias(p: Provider, part: str) -> str:
    """A legacy id (or a substring that uniquely names one — `vision-exp`)
    → the provider's current model id; anything else passes through."""
    if part in p.aliases:
        return p.aliases[part]
    hits = {old for old in p.aliases if part.lower() in old.lower()}
    if len(hits) == 1 and not any(part.lower() in m.lower() for m in p.models):
        return p.aliases[hits.pop()]
    return part


def _pick_model(part: str, models: list[str]) -> Optional[str]:
    """The model *part* names, or None if it names nothing / stays ambiguous.
    Exact id wins outright; then a substring must be unique — EXCEPT the
    family case: "glm-5.3" hits both glm-5.3 and glm-5.3-flash, and the BASE
    model (the shortest hit, when every other hit merely extends it) is what
    the user meant. The variant stays reachable with a more specific part."""
    part = part.lower()
    exact = [m for m in models if part == m.lower()]
    if exact:
        return exact[0]
    hits = sorted({m for m in models if part in m.lower()}, key=len)
    if not hits:
        return None
    if len(hits) == 1 or all(hits[0] in m for m in hits[1:]):
        return hits[0]
    return None


# The starter recommended when a local server has nothing pulled yet:
# tool-capable, MLX-served on Apple Silicon (Ollama ≥0.32.12 ships the tag),
# and the model rocky's local support was tested against.
RECOMMENDED_LOCAL_MODEL = "qwen3.8:27b-mlx"


def _local_state(p: Provider) -> Optional[str]:
    """Why local provider *p* can offer no models right now: "down", "empty",
    or None when it has models (or isn't local)."""
    if not p.local or p.models:
        return None
    _, up = local_models(p.endpoints[0].base_url)
    return "empty" if up else "down"


def local_hint(spec: str) -> Optional[str]:
    """When a /model *spec* names a LOCAL provider that can't serve it right
    now, the exact fix — instead of the generic "unknown or ambiguous". None
    when no local provider is involved (callers fall back to the generic)."""
    eid_part, model_part = split_spec(spec)
    for p in discover().values():
        if not p.local:
            continue
        if eid_part not in {p.name, *(e.eid for e in p.endpoints)}:
            continue
        state = _local_state(p)
        if state == "down":
            return (f"{p.name} isn't running — start it: `ollama serve` "
                    f"(or open the Ollama app), then `/model {spec.strip()}` again")
        if state == "empty":
            return (f"{p.name} is running but has no models pulled — "
                    f"`ollama pull {RECOMMENDED_LOCAL_MODEL}` (recommended), "
                    f"then `/model {p.name}` again")
        if model_part.strip():  # server up, models exist, but this one isn't pulled
            return (f"'{model_part.strip()}' isn't pulled — `ollama pull "
                    f"{model_part.strip()}`, then `/model {spec.strip()}` again · "
                    f"pulled now: {', '.join(p.models[:6])}")
    return None


def local_hints() -> list[str]:
    """One picker row per local provider with nothing to offer right now —
    shown dimmed so local models stay DISCOVERABLE instead of silently absent."""
    out = []
    for p in discover().values():
        state = _local_state(p)
        if state == "down":
            out.append(f"{p.name} — not running · start it: ollama serve")
        elif state == "empty":
            out.append(f"{p.name} — no models pulled · ollama pull {RECOMMENDED_LOCAL_MODEL}")
    return out


def refused_rows() -> dict[str, str]:
    """Model rows the registry will not load, and why. A refused row is said out
    loud wherever its model is asked for, instead of the model quietly vanishing
    or running a contract nobody wrote."""
    data = registry_data()
    live = {name for name, cfg in (data.get("providers") or {}).items()
            if isinstance(cfg, dict) and not cfg.get("hidden") and str(cfg.get("base_url") or "").strip()}
    out: dict[str, str] = {}
    for mid, m in (data.get("models") or {}).items():
        if not isinstance(m, dict) or m.get("hidden") or str(m.get("provider") or "") not in live:
            continue
        try:
            _model_spec(str(mid), m, str(m["provider"]))
        except ValueError as e:
            out[str(mid)] = str(e)
    return out


def _tilde(path: Path) -> str:
    return str(path).replace(str(Path.home()), "~", 1)


def refused_hint(spec: str) -> Optional[str]:
    """When a /model *spec* names a row the registry refused: what is wrong
    with the row and where to fix it. Exact ids only — a refused row must not
    capture a request for some other unlisted model."""
    rows = refused_rows()
    if not rows:
        return None
    asked = {spec.strip().lower(), split_spec(spec)[1].lower()}
    # A legacy id is a request for the row it now names.
    for cfg in (registry_data().get("providers") or {}).values():
        aliases = cfg.get("aliases") if isinstance(cfg, dict) else None
        asked |= {str(new).lower() for old, new in (aliases or {}).items() if str(old).lower() in asked}
    hit = next((mid for mid in rows if mid.lower() in asked), None)
    if hit is None:
        return None
    return (f"the row for '{hit}' was refused: {rows[hit]}. Fix it in {_tilde(MODELS_TOML)} "
            f"— every field is explained at the top of {_tilde(BUILTIN_MODELS_TOML)}")


def refused_hints() -> list[str]:
    """One picker row per refused model row, so the model stays visible with its reason."""
    return [f"{mid} — row refused · {why}" for mid, why in refused_rows().items()]


def fmt_tokens(n: int) -> str:
    """1_048_576 → 1M · 384_000 → 384K · 65_536 → 64K (picker / status text)."""
    # decimal when the number is a round decimal (384000 → 384K, the way the
    # provider prints it), binary otherwise (65536 → 64K, 1048576 → 1M)
    if n >= 1_000_000:
        v = n / 1_000_000 if n % 1000 == 0 else n / 1_048_576
        return f"{v:.0f}M" if abs(v - round(v)) < 0.05 else f"{v:.1f}M"
    if n >= 1_000:
        v = n / 1000 if n % 1000 == 0 else n / 1024
        return f"{v:.0f}K"
    return str(n)
