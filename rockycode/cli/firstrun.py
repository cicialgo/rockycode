"""First run: say where your models come from, add one key, start.

Any provider in the catalog (plan rows included), a local Ollama with no key
at all, or your own OpenAI-compatible URL. It used to ask for a DeepSeek key
whatever you were about to run. When a named model is only missing its key,
the flow asks for that one key and nothing else.

The credential chain is rockycode/onboarding.py (save_key keeps every other
line of the key file, or puts the key in the OS keychain when the machine has
one and the user says yes; a keychain that will not take it saves nothing, and
the user is asked again); where a key is sent is engine/route.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import typer

from rockycode import onboarding
from rockycode.onboarding import probe_key  # noqa: F401 — patched here by the flow's tests
from rockycode.banner import amaze, info, show_banner
from rockycode.engine import providers


@dataclass
class Row:
    label: str
    provider: providers.Provider
    endpoint: providers.Endpoint

    @property
    def models(self) -> list[str]:
        return [m for m in self.provider.models if self.endpoint in self.provider.endpoints_for(m)]


def rows() -> list[Row]:
    """One row per endpoint a first key can be for, in catalog order. A local
    server is listed even with nothing pulled: picking it says what to do."""
    found = [Row(p.label + (f" · {e.label}" if e.label else ""), p, e)
             for p in providers.discover().values() for e in p.endpoints]
    return [row for row in found if row.models or row.provider.local]


def _ask(text: str, *, default: Optional[str] = None, hide: bool = False) -> str:
    return typer.prompt(text, default=default, hide_input=hide, show_default=default is not None).strip()


def _yes(text: str, *, default: bool = True) -> bool:
    return typer.confirm(text, default=default)


def _tested_key(console, what: str, base_url: str) -> str:
    """Ask for a key until the endpoint stops rejecting it."""
    key = _ask(f"  paste your {what} API key", hide=True)
    while True:
        while not key:
            key = _ask("  a key is required — paste it", hide=True)
        console.print(f"  [dim]testing the key — one free call to {base_url}/models (no tokens billed)…[/]")
        verdict = probe_key(key, base_url)
        if verdict != "rejected":
            break
        info(console, "that key was rejected by the endpoint (401) — check for typos or stray spaces.")
        key = _ask("  paste your API key again", hide=True)
    if verdict == "ok":
        amaze(console, "key tested and accepted — it works!")
    else:
        info(console, f"couldn't reach {base_url} to test the key (offline?) — saving anyway. "
                      "if chat fails with 401, add a fresh key the same way.")
    return key


def _keychain(console) -> bool:
    """Asked only where there is an OS keychain to keep the key in."""
    return onboarding.keychain_available() and _yes(
        "  keep the key in your OS keychain (encrypted — safer than a file)?")


def _store_key(console, name: str, key: str) -> None:
    """Save one key where the user says. The keychain did not take it: nothing
    is saved, and they pick again — the key file is plain text, so it is never
    the silent second choice."""
    keychain = _keychain(console)
    while True:
        try:
            where = onboarding.save_key(name, key, prefer_keyring=keychain)
            break
        except onboarding.KeychainRefused:
            info(console, "the OS keychain did not take the key: it may be locked, or this session "
                          "(ssh, a scheduled job) cannot open it. nothing is saved yet.")
            keychain = _numbered(console, [
                "try the OS keychain again  [dim]unlock it first[/]",
                "keep the key in the key file  [dim]plain text, readable only by your user[/]"]) == 0
    info(console, f"key saved to {where} as {name}.")


def _numbered(console, options: list[str], *, default: int = 1) -> int:
    for i, text in enumerate(options, 1):
        console.print(f"  [dim]{i:>2}[/]  {text}")
    while True:
        raw = _ask("  pick a number", default=str(default))
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        info(console, f"a number from 1 to {len(options)}, please.")


def _add_key(console, row: Row) -> None:
    """One key for one endpoint, under that endpoint's own name."""
    key = _tested_key(console, row.label, row.endpoint.base_url)
    _store_key(console, row.endpoint.key_env, key)


def _default_model(console, row: Row, model: str) -> None:
    spec = f"{row.endpoint.eid}:{model}"
    onboarding.save_setting("ROCKYCODE_MODEL", spec)
    info(console, f"default model: {spec}  [dim](change it anytime with /model or `rockycode config model`)[/]")


def _cloud(console, row: Row) -> bool:
    if row.endpoint.key() is None:
        _add_key(console, row)
    else:
        info(console, f"{row.label} already has a key ({row.endpoint.key_source()}).")
    models = row.models
    labels = [f"{m}  [dim]{row.provider.spec(m).label}[/]" for m in models]
    _default_model(console, row, models[_numbered(console, labels)] if len(models) > 1 else models[0])
    return True


def _local(console, row: Row) -> bool:
    """A local server needs no key, only a model that can take the job."""
    from rockycode.engine.localcheck import preflight
    models, up = providers.local_models(row.endpoint.base_url)
    if not up:
        info(console, f"{row.provider.name} isn't running — start it (`ollama serve`, or open the Ollama "
                      "app), then pick it again.")
        return False
    if not models:
        info(console, f"{row.provider.name} is running but has no models — "
                      f"`ollama pull {providers.RECOMMENDED_LOCAL_MODEL}` first, then pick it again.")
        return False
    model = models[_numbered(console, models)] if len(models) > 1 else models[0]
    check = preflight(row.endpoint.base_url, model)
    for c in check.checks:
        if c.status in ("warn", "fail"):
            info(console, f"{c.text}" + (f"  [dim]fix: {c.fix}[/]" if c.fix else ""))
    if not check.ok:
        info(console, f"{model} is not ready for rocky yet — pick another, or fix the above.")
        return False
    _default_model(console, row, model)
    return True


def _own_url(console) -> bool:
    """Your own OpenAI-compatible endpoint: a proxy, a gateway, a self-hosted
    server. It becomes rocky's home endpoint (ROCKYCODE_BASE_URL)."""
    base_url = _ask("  base URL (it usually ends in /v1)")
    while not base_url.startswith(("http://", "https://")):
        base_url = _ask("  a URL starting with http:// or https://")
    key = _tested_key(console, "endpoint's", base_url)
    model = _ask("  model id to use there")
    while not model:
        model = _ask("  a model id is required")
    _store_key(console, onboarding.KEY_ENV, key)
    onboarding.save_setting(onboarding.BASE_URL_ENV, base_url)
    onboarding.save_setting("ROCKYCODE_MODEL", model)
    info(console, f"saved: {onboarding.BASE_URL_ENV} and the default model {model}.")
    return True


def _language(console) -> None:
    # Reply language — asked once so a 中文 user's first session already
    # answers in Chinese instead of depending on model luck. auto mirrors
    # whatever language each message is written in. Changeable anytime:
    # `rockycode config language zh` or /config in the TUI.
    lang = _ask("  reply language / 回复语言 (auto = follow my messages · en · zh)", default="auto").lower()
    if lang in {"中文", "cn", "chinese"}:
        lang = "zh"
    elif lang in {"english"}:
        lang = "en"
    if lang not in {"auto", "en", "zh"}:
        info(console, f"'{lang}' not recognized — using auto (follows your messages).")
        lang = "auto"
    from rockycode.config import set_value
    _, lang_err = set_value("language", lang)
    if lang_err:
        info(console, f"could not save language: {lang_err}")


def run_setup(console, missing=None) -> None:
    """The interactive first run. *missing* is the RouteError of a model the
    user named whose endpoint has no key yet: then only that key is asked for."""
    catalog = rows()
    wanted = next((r for r in catalog if missing is not None and missing.endpoint is not None
                   and r.endpoint.eid == missing.endpoint.eid), None)
    if wanted is not None:
        # A key the keychain holds but did not give (locked, ssh) is not a key never added.
        info(console, f"no key for {wanted.label} yet — let's add it."
                      + onboarding.keychain_note(wanted.endpoint.key_env))
        _add_key(console, wanted)
        console.print()
        return
    show_banner(console)
    console.print()
    info(console, "first run — where do your models come from?")
    console.print("  [dim]one is enough to start; /model adds the rest later.[/]")
    options = [r.label + ("  [dim]no key needed[/]" if r.endpoint.keyless else "") for r in catalog]
    options.append("my own URL  [dim]any OpenAI-compatible endpoint[/]")
    while True:
        picked = _numbered(console, options)
        if picked == len(catalog):
            done = _own_url(console)
        elif catalog[picked].provider.local:
            done = _local(console, catalog[picked])
        else:
            done = _cloud(console, catalog[picked])
        if done:
            break
        console.print()
    _language(console)
    info(console, "you're set. `rockycode` works in any project now.")
    console.print()
