"""rocky configures rocky: the `rocky_config` tool.

A user who doesn't know where a base URL goes, which key name a provider
wants, or how to add their own vLLM box shouldn't have to learn rocky's file
layout — they ask rocky. This tool is rocky's ONLY hand on its own settings:

  show          what is set now (config keys, providers + endpoints + whether
                each key NAME is present — never a value — the models the
                registry knows, the files involved)
  set           a config key (config.DEFAULTS): model, language, ui_language, currency,
                image, context_window, max_tokens, …
  set_url       an own base URL for a provider (~/.rockycode/endpoints.toml →
                `<provider>-custom`); empty url removes it
  add_provider  a custom OpenAI-compatible server (~/.rockycode/models.toml):
                name, base_url, model ids, optional reasoning shape / vision /
                limits — the key stays a NAME (ROCKYCODE_<NAME>_API_KEY)

Every write lands under ~/.rockycode only, through the same validated
setters the CLI uses. A write is the user's decision every time: it asks in
every permission mode, yolo included, an "allow for this session" never
covers it, and the card shows the whole change in plain words
(permission.human_only, approval.tool_detail).

Three things the tool does not do whoever approves, because each decides what
rocky may do later without asking (_user_only): set a command as the image
describer (a program that would run on the user's machine), loosen the
approval mode, or add a server under a catalog provider's name (that
provider's saved key would go to the new address). The user types those.

Keys never pass through here: an argument that looks like a secret is
refused with the place to add it instead. The built-in `rocky-setup` skill
teaches the model the shapes.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from rockycode.engine.tools import Tool, _fn_schema

# Anything that smells like a credential: the common prefixes, or a long
# opaque token. False positives cost one refusal; a leak costs a rotation.
_SECRET_RE = re.compile(
    r"(?i)\b(?:sk|key|token|secret|pat|ghp|xox[abp])[-_][A-Za-z0-9_\-]{12,}"
    r"|(?<![\w/.:-])[A-Za-z0-9_\-]{40,}(?![\w/.:-])"
)

SCHEMA = _fn_schema(
    "rocky_config",
    "Change rocky's OWN settings for the user (never the project's). Actions: "
    "show (current config, providers, endpoints, key NAMES set or not, known "
    "models) · set (a config key: model, language, ui_language, currency, image, "
    "context_window, max_tokens, …) · set_url (an own base URL for a "
    "provider — a proxy/gateway/plan URL; empty url removes it) · add_provider "
    "(a custom OpenAI-compatible server: a NEW name + base_url + model ids). Writes "
    "go under ~/.rockycode only, and the user approves every one. You cannot set a "
    "command as `image`, loosen `permission`, or reuse a catalog provider's name: "
    "the user types those. NEVER pass an API key here — the user adds it in the "
    "app (/model → add or change a key).",
    {
        "action": {"type": "string", "enum": ["show", "set", "set_url", "add_provider"]},
        "key": {"type": "string", "description": "set: the config key."},
        "value": {"type": "string", "description": "set: the new value."},
        "provider": {"type": "string",
                     "description": "set_url / add_provider: provider name (deepseek, kimi, my-vllm)."},
        "base_url": {"type": "string",
                     "description": "set_url / add_provider: the OpenAI-compatible base URL (…/v1)."},
        "models": {"type": "string",
                   "description": "add_provider: model ids, comma-separated."},
        "reasoning": {"type": "string",
                      "description": "add_provider: wire shape — thinking | effort | enable_thinking | minimax | openai | none (default openai)."},
        "vision": {"type": "boolean", "description": "add_provider: the models take image input."},
        "context": {"type": "integer", "description": "add_provider: context window in tokens (optional)."},
        "max_output": {"type": "integer", "description": "add_provider: max output tokens (optional)."},
    },
    ["action"],
)


def _looks_secret(*values: Optional[str]) -> bool:
    return any(v and _SECRET_RE.search(v) for v in values)


_REFUSE_KEY = ("[refused] that looks like an API key — keys never go through chat or "
               "into rocky's config files. The user adds it in a hidden field: `/model` → "
               "\"add or change a key\" in the TUI, or the key form in the desktop app "
               "(it goes to the OS keychain where there is one). Its name is {env}; then "
               "`/model {provider}` works. rocky reads only rocky-owned ROCKYCODE_* names, "
               "never an ambient provider key.")


def _user_only(action: str, key: str, value: str, provider: str) -> str:
    """The changes rocky does not make for the user, whoever approves — or "".
    Each decides what rocky may do later without asking, so the user types it."""
    from rockycode import config as C
    from rockycode.engine import providers as P
    from rockycode.engine import vision
    if action == "set" and key == "image":
        setting = vision.image_setting(C._coerce(key, value))
        if setting.route == "cli" and setting.template not in vision.KNOWN_CLIS:
            return ("[refused] an image command is a program that would run on the user's machine for "
                    "every image, so rocky does not set one. The user types it themselves: `/config "
                    "image <command>` in the TUI, or `rockycode config image \"<command>\"` in a "
                    "terminal. You can set: auto, ask, off, a keyed vision endpoint (kimi, "
                    f"kimi:kimi-k3), or a known tool by name ({', '.join(sorted(vision.KNOWN_CLIS))}).")
    if action == "set" and key == "permission":
        current, wanted = C.global_value("permission"), C._coerce(key, value)
        if C._PERM_RANK.get(wanted, 9) < C._PERM_RANK[current]:
            return (f"[refused] rocky does not loosen its own approval mode ({current} → {wanted}). "
                    "The user does it themselves: the approval chip in the status bar for this "
                    f"session, or `/config permission {wanted}` to keep it.")
    if action == "add_provider" and provider:
        owner = P.catalog_owner(provider)
        if owner:
            return (f"[refused] {provider!r} belongs to the catalog provider {owner}: a server added "
                    f"under that name would receive the key saved for {owner}. Pick a new name for "
                    f"the server, or give {owner} an own address with set_url (the user approves it).")
    return ""


def _show(workdir: Path) -> str:
    from rockycode.config import DEFAULTS, GLOBAL_PATH, load
    from rockycode.engine import providers as P
    cfg = load(workdir)
    lines = [f"config ({GLOBAL_PATH}):"]
    lines += [f"  {k} = {cfg[k]!r}" for k in DEFAULTS]
    lines.append("")
    lines.append("providers (models.toml + override; keys are NAMES only):")
    for prov in P.discover().values():
        spec_bits = []
        for m in prov.models[:8]:
            spec_bits.append(f"{m}{' ❖' if P.Choice(prov, prov.endpoints[0], m).vision else ''}")
        lines.append(f"  {prov.name}: reasoning={prov.reasoning} · models: "
                     f"{', '.join(spec_bits) or '(none discovered)'}")
        for ep in prov.endpoints:
            if ep.keyless:
                keyed = "no key needed"
            else:
                src = ep.key_source()
                keyed = f"key set ({src})" if src else f"key NOT set — {ep.key_env}"
            lines.append(f"    {ep.eid}: {ep.base_url} · {keyed}")
    lines.append("")
    lines.append(f"files: registry override {P.MODELS_TOML} · own URLs {P.ENDPOINTS_TOML} · "
                 f"custom servers {P.PROVIDERS_TOML} · keys: the OS keychain, or ~/.rockycode/.env")
    return "\n".join(lines)


def build_selfconfig_tool(workdir: Path) -> Tool:
    async def fn(action: str, key: str = "", value: str = "", provider: str = "",
                 base_url: str = "", models: str = "", reasoning: str = "openai",
                 vision: bool = False, context: int = 0, max_output: int = 0) -> str:
        from rockycode import config as C
        from rockycode.engine import providers as P
        action = (action or "").strip().lower()
        if action == "show":
            return _show(workdir)
        if _looks_secret(value, base_url, models, key, provider):
            env = (f"ROCKYCODE_{re.sub(r'[^A-Z0-9]', '_', (provider or 'provider').upper())}_API_KEY"
                   if provider and provider != "deepseek" else "ROCKYCODE_API_KEY")
            return _REFUSE_KEY.format(env=env, provider=provider or "deepseek")
        from rockycode.engine.approval import SETTING_SHOWN
        if any(len(str(v or "")) > SETTING_SHOWN for v in (key, value, provider, base_url, models)):
            return ("[refused] that value is too long to show in full on the approval card, "
                    "and the user approves only what they can read.")
        refused = _user_only(action, key.strip(), value, provider)
        if refused:
            return refused
        if action == "set":
            if not key:
                return "[error] set needs key + value"
            v, err = C.set_value(key, value)
            if err:
                return f"[error] {err}"
            note = ""
            if key == "model":
                note = " — applies at next launch; /model switches this session"
            elif key in ("context_window", "max_tokens"):
                note = " — 0 means follow the model's registry value"
            feedback = C.change_feedback(key, workdir)
            if feedback:
                note += f" — {feedback}"
            return f"[ok] {key} = {v!r} saved to {C.GLOBAL_PATH}{note}"
        if action == "set_url":
            if not provider:
                return "[error] set_url needs provider (+ base_url; empty removes)"
            if provider not in P.discover():
                return (f"[error] unknown provider {provider!r} — known: "
                        f"{', '.join(P.discover())}. A brand-new server is add_provider.")
            eid, err = P.set_custom_url(provider, base_url)
            if err:
                return f"[error] {err}"
            if eid is None:
                return f"[ok] removed the own URL for {provider} ({P.ENDPOINTS_TOML})"
            return (f"[ok] {eid} → {base_url} saved to {P.ENDPOINTS_TOML}; it rides "
                    f"{provider}'s key. switch with /model {eid} (this session) or "
                    f"`rocky_config set model {eid}` (default)")
        if action == "add_provider":
            if not (provider and base_url and models):
                return "[error] add_provider needs provider, base_url, models"
            ids = [m.strip() for m in re.split(r"[,\s]+", models) if m.strip()]
            name, err = P.add_provider(provider, base_url, ids, reasoning=reasoning or "openai",
                                       vision=bool(vision), context=int(context or 0),
                                       max_output=int(max_output or 0))
            if err:
                return f"[error] {err}"
            prov = P.discover().get(name)
            env = prov.endpoints[0].key_env if prov else f"ROCKYCODE_{name.upper()}_API_KEY"
            return (f"[ok] provider {name} ({base_url}; models {', '.join(ids)}) saved to "
                    f"{P.MODELS_TOML}. its key is named {env}: the user adds it in the app "
                    f"(/model → add or change a key — never here); then /model {name}")
        return "[error] action must be show | set | set_url | add_provider"

    return Tool(name="rocky_config", schema=SCHEMA, fn=fn, risk="risky")
