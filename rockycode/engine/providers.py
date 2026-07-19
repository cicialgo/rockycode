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
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rockycode.onboarding import BASE_URL_ENV, DEFAULT_BASE_URL, KEY_ENV

_HOME = Path(os.environ.get("ROCKYCODE_HOME") or Path.home() / ".rockycode")
PROVIDERS_TOML = _HOME / "providers.toml"
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


def _builtins() -> dict[str, Provider]:
    # NOTE: regional base_urls are best-effort — verify per provider when you
    # first test one; correcting a URL is a one-line data edit here or in TOML.
    return {
        "deepseek": Provider(
            name="deepseek", models=["deepseek-v4-pro", "deepseek-v4-flash"],
            endpoints=[Endpoint("deepseek",
                                (os.getenv(BASE_URL_ENV) or "").strip() or DEFAULT_BASE_URL,
                                KEY_ENV)],
            reasoning="deepseek", label="DeepSeek V4 — rocky's home model"),
        "minimax": Provider(
            name="minimax", models=["minimax-m3"],
            endpoints=[
                Endpoint("minimax-en", "https://api.minimaxi.chat/v1", "ROCKYCODE_MINIMAX_EN_API_KEY"),
                Endpoint("minimax-cn", "https://api.minimax.chat/v1", "ROCKYCODE_MINIMAX_CN_API_KEY"),
            ],
            reasoning="openai", label="MiniMax M3"),
        "kimi": Provider(
            name="kimi", models=["kimi-k3"],
            endpoints=[
                Endpoint("kimi-en", "https://api.moonshot.ai/v1", "ROCKYCODE_KIMI_EN_API_KEY"),
                Endpoint("kimi-cn", "https://api.moonshot.cn/v1", "ROCKYCODE_KIMI_CN_API_KEY"),
            ],
            reasoning="none", label="Kimi / Moonshot"),
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
                             label=cfg.get("label", ""), builtin=False)
    return out


def discover() -> dict[str, Provider]:
    providers = _builtins()
    providers.update(_parse_toml(PROVIDERS_TOML))
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
        hits = [m for m in p.models if model_part.lower() in m.lower()]
        return (p, e, hits[0]) if len(hits) == 1 else None

    # bare model substring — must be unique across all providers (first endpoint)
    matches = [(p, p.endpoints[0], m) for p in discover().values() for m in p.models
               if spec.lower() in m.lower()]
    return matches[0] if len(matches) == 1 else None
