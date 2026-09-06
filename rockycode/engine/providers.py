"""Provider profiles: switch which OpenAI-compatible endpoint + model rocky uses.

Anti-glue by design. Every provider here (DeepSeek, MiniMax, GLM, Kimi, …)
speaks the OpenAI chat-completions protocol, so the OpenAI SDK is the ONLY
compatibility layer — rocky never writes a per-provider adapter. A provider is
DATA, not code: a base_url, some model ids, which env var holds its key, and
which reasoning-param shape it wants. Adding one is a few lines here or in
`~/.rockycode/providers.toml`; a non-OpenAI-compatible provider is unsupported.

Regions: MiniMax / Kimi / GLM run SEPARATE international and China endpoints —
different base_url AND different key. That's modeled as multiple `endpoints` on
one provider (models shared), not duplicated entries. Each endpoint is pickable
as `<provider>` (the default) or `<provider>-<region>` (e.g. `kimi-cn`).

Keys are rocky-OWNED per endpoint: `ROCKYCODE_<NAME>_API_KEY` (intl) /
`ROCKYCODE_<NAME>_CN_API_KEY` (cn) — never the ambient `MINIMAX_API_KEY` etc.
The built-in `deepseek` reads the existing ROCKYCODE_API_KEY, so current setups
keep working with no new key.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rockycode.onboarding import BASE_URL_ENV, DEFAULT_BASE_URL, KEY_ENV

_HOME = Path(os.environ.get("ROCKYCODE_HOME") or Path.home() / ".rockycode")
PROVIDERS_TOML = _HOME / "providers.toml"
# Own-URL overrides (proxies, self-hosted gateways): a flat, rocky-OWNED file —
# `<provider> = "<base_url>"` — that discover() turns into an extra
# `<provider>-custom` endpoint on the builtin. Separate from providers.toml so
# rocky can rewrite it safely (the picker's "custom base URL" row writes here)
# without ever touching a hand-maintained file.
ENDPOINTS_TOML = _HOME / "endpoints.toml"
_PLACEHOLDERS = {"", "replace-me", "sk-replace-me", "your-api-key"}


@dataclass
class Endpoint:
    # `eid` is the EXPLICIT /model token — no magic default. Regional endpoints
    # are spelled out (kimi-en / kimi-cn / minimax-en / minimax-cn), and where a
    # provider's international arm is a different brand it gets that name (GLM's
    # is z.ai → `zai`). `key_env` matches: ROCKYCODE_<EID_UPPER>_API_KEY.
    eid: str
    base_url: str
    key_env: str

    def key(self) -> Optional[str]:
        v = (os.getenv(self.key_env) or "").strip()
        return v if v and v.lower() not in _PLACEHOLDERS else None


@dataclass
class Provider:
    name: str
    models: list[str]
    endpoints: list[Endpoint]
    reasoning: str = "openai"   # deepseek | openai | none
    tools: str = "native"      # native | off
    # vision=True: EVERY listed model takes image input (minimax-m3, kimi-k3,
    # step-3.7-flash). A provider mixing vision and text models (deepseek:
    # flash/pro are text, flash-vision-exp sees) lists just the seeing ones in
    # vision_models instead. Capability is per-CHOICE: Choice.vision is the one
    # question callers ask; config key `vision_models` adds names here at
    # discover() time, so "flash itself gained vision" is a config flip, not a
    # code change.
    vision: bool = False
    vision_models: list[str] = field(default_factory=list)
    label: str = ""
    builtin: bool = True


@dataclass
class Choice:
    """A flat, pickable (provider, endpoint, model). `id` is `<eid>:<model>`
    (e.g. kimi-cn:kimi-k3 / zai:glm-5.2); `configured` = its key is set, which
    is how the picker stays short (show only what you've keyed)."""
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
    def vision(self) -> bool:
        """Does THIS model take image input? Model-level (vision_models) wins;
        a provider-level vision=True still means all of them."""
        return self.provider.vision or self.model in self.provider.vision_models


def _builtins() -> dict[str, Provider]:
    # NOTE: regional base_urls are best-effort — verify per provider when you
    # first test one; correcting a URL is a one-line data edit here or in TOML.
    return {
        "deepseek": Provider(
            # flash first: it is the default pick (models[0]) for a bare
            # `/model deepseek` — the normal serving tier; pro is the preview.
            # flash-vision-exp (released 2026-08-21): flash-0731 + image input,
            # same text/agent ability + tool calling, same list price.
            name="deepseek", models=["deepseek-v4-flash", "deepseek-v4-pro",
                                     "deepseek-v4-flash-vision-exp"],
            endpoints=[Endpoint("deepseek",
                                (os.getenv(BASE_URL_ENV) or "").strip() or DEFAULT_BASE_URL,
                                KEY_ENV)],
            reasoning="deepseek", vision_models=["deepseek-v4-flash-vision-exp"],
            label="DeepSeek V4 — rocky's home model"),
        "minimax": Provider(
            name="minimax", models=["minimax-m3"],
            endpoints=[
                Endpoint("minimax-en", "https://api.minimaxi.chat/v1", "ROCKYCODE_MINIMAX_EN_API_KEY"),
                Endpoint("minimax-cn", "https://api.minimax.chat/v1", "ROCKYCODE_MINIMAX_CN_API_KEY"),
            ],
            reasoning="openai", vision=True, label="MiniMax M3"),
        "kimi": Provider(
            name="kimi", models=["kimi-k3"],
            endpoints=[
                Endpoint("kimi-en", "https://api.moonshot.ai/v1", "ROCKYCODE_KIMI_EN_API_KEY"),
                Endpoint("kimi-cn", "https://api.moonshot.cn/v1", "ROCKYCODE_KIMI_CN_API_KEY"),
            ],
            reasoning="none", vision=True, label="Kimi / Moonshot"),
        "stepfun": Provider(
            name="stepfun", models=["step-3.7-flash"],
            endpoints=[Endpoint("stepfun", "https://api.stepfun.com/v1",
                                "ROCKYCODE_STEPFUN_API_KEY")],
            reasoning="none", vision=True, label="StepFun — native multimodal"),
        "glm": Provider(
            name="glm", models=["glm-5.2"],
            endpoints=[
                # international arm is a different brand — z.ai, not "glm"
                Endpoint("zai", "https://api.z.ai/api/paas/v4", "ROCKYCODE_ZAI_EN_API_KEY"),
                Endpoint("glm-cn", "https://open.bigmodel.cn/api/paas/v4", "ROCKYCODE_GLM_CN_API_KEY"),
            ],
            reasoning="none", label="GLM (z.ai intl / bigmodel.cn)"),
    }


def _parse_toml(path: Path) -> dict[str, Provider]:
    try:
        import tomllib
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 — a broken file must not crash startup
        return {}
    out: dict[str, Provider] = {}
    for name, cfg in (data.get("providers") or data).items():
        if not isinstance(cfg, dict):
            continue
        models = cfg.get("models") or ([cfg["model"]] if cfg.get("model") else [])
        # endpoints: explicit list, or a single base_url/key_env pair. Each
        # endpoint's `id` is its /model token; key_env defaults to
        # ROCKYCODE_<ID_UPPER>_API_KEY.
        def _kenv(eid: str) -> str:
            return f"ROCKYCODE_{eid.upper().replace('-', '_')}_API_KEY"
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
        out[name] = Provider(name=name, models=list(models), endpoints=eps,
                             reasoning=cfg.get("reasoning", "openai"),
                             tools=cfg.get("tools", "native"),
                             vision=bool(cfg.get("vision", False)),
                             vision_models=[str(m) for m in cfg.get("vision_models") or []],
                             label=cfg.get("label", ""), builtin=False)
    return out


def _custom_urls() -> dict[str, str]:
    """~/.rockycode/endpoints.toml: flat `<provider> = "<base_url>"` lines."""
    try:
        import tomllib
        data = tomllib.loads(ENDPOINTS_TOML.read_text())
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 — a broken file must not crash startup
        return {}
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


def _config_vision_models() -> list[str]:
    # Config key `vision_models`: extra model ids to treat as image-capable —
    # for when a provider ships vision on an EXISTING id (flash itself gains
    # vision) before this registry catches up. Scriptable from any shell
    # (another rocky included): `rockycode config vision_models <id>[,<id>…]`.
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
            p.endpoints.append(Endpoint(f"{name}-custom", url, p.endpoints[0].key_env))
    extra = _config_vision_models()
    if extra:
        for p in providers.values():
            p.vision_models = p.vision_models + [
                m for m in extra if m in p.models and m not in p.vision_models]
    return providers


def choices() -> list[Choice]:
    """Every (provider, endpoint, model) as a flat pickable list."""
    return [Choice(p, e, m) for p in discover().values()
            for e in p.endpoints for m in p.models]


def configured_choices() -> list[Choice]:
    """Only choices whose key is set — the SHORT list the picker shows by
    default, so an EN/CN catalog of ~12 doesn't scroll. deepseek is always
    included (it rides the default ROCKYCODE_API_KEY)."""
    out = [c for c in choices() if c.configured or c.provider.name == "deepseek"]
    return out


def resolve(spec: str) -> Optional[tuple[Provider, Endpoint, str]]:
    """Resolve a /model arg to (provider, endpoint, model).

    Forms: an endpoint id (`deepseek`, `kimi-cn`, `zai`), `endpoint:model`
    (`kimi-cn:k3`, `zai:glm`), or a unique bare model substring. Returns None
    if the endpoint is unknown or the model substring is ambiguous.
    """
    spec = spec.strip()
    eid_part, _, model_part = spec.replace(":", " ").partition(" ")
    eid_part, model_part = eid_part.strip(), model_part.strip()

    by_eid = {e.eid: (p, e) for p in discover().values() for e in p.endpoints}
    if eid_part in by_eid:
        p, e = by_eid[eid_part]
        if not model_part:
            return p, e, p.models[0]
        m = _pick_model(model_part, p.models)
        return (p, e, m) if m else None

    # bare model — must pick uniquely across all providers (first endpoint)
    all_models = [m for prov in discover().values() for m in prov.models]
    m = _pick_model(spec, all_models)
    if m is None:
        return None
    for prov in discover().values():
        if m in prov.models:
            return prov, prov.endpoints[0], m
    return None


def _pick_model(part: str, models: list[str]) -> Optional[str]:
    """The model *part* names, or None if it names nothing / stays ambiguous.
    Exact id wins outright; then a substring must be unique — EXCEPT the
    family case: "flash" hits both deepseek-v4-flash and its longer
    -vision-exp variant, and the BASE model (the shortest hit, when every
    other hit merely extends it) is what the user meant. The variant stays
    reachable with a more specific part ("vision")."""
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
