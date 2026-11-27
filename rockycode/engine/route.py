"""Where a model's requests go, and with which key — decided once, here.

Every entry point that talks to a model asks this module: the chat launch, a
/model switch, exec, goal, dream, the bench runners, the MCP server, the
desktop, the title and image sidecars, native search. It used to be re-derived
at each of them with four different fallback rules, which is how `goal` came to
ignore the model's own endpoint and `exec` came to demand a key the task never
used.

The rule:

1. A model the registry knows goes to ITS endpoint with THAT endpoint's key.
   A local server needs none.
2. A missing key sends nothing anywhere else: the error names the variable to
   set. One exception, and only where the user built it — with
   ROCKYCODE_BASE_URL pointed at their own gateway, a model without its own key
   rides the gateway and the gateway's key. A plan-only model never does.
3. An id the registry does not know belongs to the home endpoint
   (ROCKYCODE_BASE_URL + ROCKYCODE_API_KEY): a new id, a fine-tune, a gateway's
   own alias.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

from rockycode.engine import providers
from rockycode.onboarding import (BASE_URL_ENV, DEFAULT_BASE_URL, KEY_ENV, NO_KEY, current_key,
                                  current_key_source, keychain_note, require_base_url)


class RouteError(RuntimeError):
    """No usable route. The message is the fix. `key_env` names the variable
    when a key is all that is missing (first-run setup offers to take it)."""

    def __init__(self, message: str, *, key_env: str = "", endpoint: Optional[providers.Endpoint] = None):
        super().__init__(message)
        self.key_env = key_env
        self.endpoint = endpoint


@dataclass(frozen=True)
class Route:
    model: str                    # the registry id, or the id as typed when the registry has no such model
    base_url: str
    key: str = field(repr=False)  # goes to the client and nowhere else: never printed, never logged
    key_env: str = ""             # NAME of the variable the key came from; "" = a keyless local server
    via: str = "endpoint"         # endpoint | gateway | home
    provider: Optional[providers.Provider] = None
    endpoint: Optional[providers.Endpoint] = None

    @property
    def known(self) -> bool:
        """Is this a registry model? False = an id only the home endpoint knows."""
        return self.provider is not None

    @property
    def choice(self) -> Optional[providers.Choice]:
        return providers.Choice(self.provider, self.endpoint, self.model) if self.known else None

    @property
    def wire_model(self) -> str:
        return self.provider.api_model(self.model) if self.known else self.model

    def describe(self) -> str:
        """`<where the key came from> → <where it is sent>` for an auth-error
        line. Never the key."""
        if not self.key_env:
            source = "none (local server)"
        elif self.key_env == KEY_ENV:
            source = current_key_source() or KEY_ENV
        else:
            source = self.key_env
        return f"{source} → {self.base_url}"


def gateway() -> bool:
    """Has the user pointed rocky at their own URL? Then that URL may serve any
    model, and a model without its own key is sent there."""
    url = (os.getenv(BASE_URL_ENV) or "").strip()
    return bool(url) and urlsplit(url).hostname != urlsplit(DEFAULT_BASE_URL).hostname


def _home(model: str, *, via: str, provider=None, endpoint=None) -> Optional[Route]:
    key = current_key()
    if key is None:
        return None
    return Route(model, require_base_url(), key, KEY_ENV, via, provider, endpoint)


def bind(provider: providers.Provider, endpoint: providers.Endpoint, model: str, *,
         gateway_ok: bool = True) -> Route:
    """The route for an already-resolved (provider, endpoint, model).
    `gateway_ok=False` is for callers whose contract is "this endpoint or
    nothing" (the MCP server's host-selected models, the sidecars)."""
    # Every key is in the process env by now (bootstrap fills it from the key file and the
    # OS keychain); the home key also has a direct keychain read for library use.
    key = current_key() if endpoint.key_env == KEY_ENV else endpoint.key()
    if key is not None:
        return Route(model, endpoint.base_url, key, endpoint.key_source() or endpoint.key_env,
                     "endpoint", provider, endpoint)
    if gateway_ok and gateway() and not provider.spec(model).endpoint_ids:
        via_gateway = _home(model, via="gateway", provider=provider, endpoint=endpoint)
        if via_gateway is not None:
            return via_gateway
    if endpoint.key_env == KEY_ENV:
        raise RouteError(NO_KEY + keychain_note(KEY_ENV), key_env=KEY_ENV, endpoint=endpoint)
    raise RouteError(
        f"no key for {endpoint.eid}:{model} — set {endpoint.key_env} in ~/.rockycode/.env "
        f"(or export it). rocky reads only rocky-owned {endpoint.key_env}, never an "
        "ambient provider key, and never sends this model to another provider."
        + keychain_note(endpoint.key_env),
        key_env=endpoint.key_env, endpoint=endpoint)


def route(spec: str, *, gateway_ok: bool = True) -> Route:
    """The route for a /model-style spec, or a RouteError that says what to do."""
    hit = providers.resolve(spec)
    if hit is not None:
        return bind(*hit, gateway_ok=gateway_ok)
    # A local server that is down, empty or lacks this model; or a catalog row
    # the registry refused (a misspelt switch) — each with its own fix.
    hint = providers.local_hint(spec) or providers.refused_hint(spec)
    if hint:
        raise RouteError(hint)
    eid, part = providers.split_spec(spec)
    if part and any(e.eid == eid for p in providers.discover().values() for e in p.endpoints):
        # `kimi:k9` names an endpoint rocky knows and no single model on it:
        # a typo, an ambiguous part or a missing catalog row — never a request
        # for the home endpoint.
        raise RouteError(f"'{part}' is not exactly one of {eid}'s models — `/model {eid}` lists "
                         "them; a new id needs a row in ~/.rockycode/models.toml.")
    home = _home(spec.strip(), via="home")
    if home is None:
        raise RouteError(
            f"'{spec.strip()}' is not a model rocky knows, and there is no home key to send an "
            f"unlisted id to — pick one with /model, or set {KEY_ENV} (and {BASE_URL_ENV} for "
            "your own endpoint)." + keychain_note(KEY_ENV), key_env=KEY_ENV)
    return home


def open_client(to: Route, *, max_retries: int = 5, timeout: float = 300.0):
    """The async client for a route. Key AND endpoint are always explicit: the
    SDK's ambient OPENAI_API_KEY / OPENAI_BASE_URL must never decide what is
    sent where. max_retries=5: the SDK default of 2 is too low for the
    flakier providers."""
    from openai import AsyncOpenAI  # deferred: this module must import cheap
    return AsyncOpenAI(api_key=to.key, base_url=to.base_url, max_retries=max_retries, timeout=timeout)


def open_sync_client(to: Route, *, max_retries: int = 5, timeout: float = 300.0):
    """The blocking twin of open_client, for the raw bench runner."""
    from openai import OpenAI
    return OpenAI(api_key=to.key, base_url=to.base_url, max_retries=max_retries, timeout=timeout)


def sidecar_client():
    """(async client, API wire id) for the registry's keyed `sidecar` model —
    the cheap flash for titles / image describe — or None when no keyed model
    carries the role."""
    c = providers.role_choice("sidecar")
    if c is None:
        return None
    to = bind(c.provider, c.endpoint, c.model, gateway_ok=False)
    return open_client(to, max_retries=2, timeout=60.0), to.wire_model
