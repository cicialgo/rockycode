"""Readiness preflight for LOCAL providers — run by the /model switch.

A cloud switch can only fail on a missing key; a local switch can fail in
ways that surface MINUTES later (server down, model not pulled, no tool
support, a silently-truncating 4k context that kills the agent loop mid-
session). So before the engine is pointed at a local endpoint, this module
checks the server the way a colleague would — and every failed check comes
with the exact command that fixes it. Not ready → the switch is refused and
NOTHING changes; ready → the switch proceeds and rocky's context_window is
paced to the server's verified reality.

Engine-layer and UI-agnostic: returns structured checks; the TUI renders
them. All calls are stdlib urllib against the user's OWN local server —
short timeouts, never raises. Ollama's native /api endpoints are used for
detail (capabilities, context) because the OpenAI-compat surface doesn't
expose them; a server without /api (LM Studio, llama.cpp) just skips those
checks honestly instead of failing them.
"""
from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field

# Below this VERIFIED serving context the agent loop cannot work (system
# prompt + tool schemas alone crowd it, and Ollama truncates silently) —
# the switch is refused with the fix. Between floor and good: warned.
CTX_FLOOR = 16_384
CTX_GOOD = 32_768
# The serve-side setting the docs recommend; also rocky's context_window
# default when the effective ctx can't be verified yet.
CTX_RECOMMENDED = 65_536
_CTX_FIX = ("export OLLAMA_CONTEXT_LENGTH=65536 and restart the server "
            "(quit the menu-bar app or ctrl-c `ollama serve`, then start it again)")

_TIMEOUT = 2.5


@dataclass
class Check:
    status: str  # "ok" | "warn" | "fail" | "info"
    text: str
    fix: str = ""  # the exact command/action that clears a warn/fail


@dataclass
class Preflight:
    model: str
    base_url: str
    checks: list[Check] = field(default_factory=list)
    # The context_window rocky should run with: the server's VERIFIED
    # effective ctx when known, else the recommended local default.
    context_window: int = CTX_RECOMMENDED
    ctx_verified: bool = False
    vision: bool = False  # server says this model takes images

    @property
    def ok(self) -> bool:
        return not any(c.status == "fail" for c in self.checks)


def _get(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — a missing native API is a normal state
        return None


def _post(url: str, body: dict) -> dict | None:
    try:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def preflight(base_url: str, model: str) -> Preflight:
    """Check that *model* served at *base_url* (an OpenAI-compat /v1 URL) is
    actually ready to run an agent session. Blocking (call in a thread)."""
    pf = Preflight(model=model, base_url=base_url)
    root = base_url.rstrip("/").removesuffix("/v1")

    # 1 · server reachable — the OpenAI surface rocky will actually talk to
    listing = _get(base_url.rstrip("/") + "/models")
    if listing is None:
        pf.checks.append(Check(
            "fail", f"server not reachable at {base_url}",
            "start it: `ollama serve` (or open the Ollama app) · "
            "not installed? https://ollama.com/download"))
        return pf
    ver = _get(root + "/api/version") or {}
    up = "server up" + (f" (ollama v{ver['version']})" if ver.get("version") else "")
    pf.checks.append(Check("ok", f"{up} — {base_url}"))

    # 2 · model pulled — exact id in the live listing
    pulled = [str(m["id"]) for m in (listing.get("data") or [])
              if isinstance(m, dict) and m.get("id")]
    if model not in pulled:
        have = f" · pulled now: {', '.join(pulled[:6])}" if pulled else ""
        pf.checks.append(Check(
            "fail", f"model {model} is not pulled{have}",
            f"`ollama pull {model}`, then switch again"))
        return pf
    pf.checks.append(Check("ok", f"model {model} is pulled"))

    # 3 · capabilities — the agent loop NEEDS tool calling
    show = _post(root + "/api/show", {"model": model})
    caps = [str(c) for c in (show or {}).get("capabilities") or []]
    if caps and "tools" not in caps:
        pf.checks.append(Check(
            "fail", f"{model} has no tool-calling support — rocky's agent "
                    "loop can't run on it",
            "pick a tool-capable model (`ollama pull qwen3.8:27b-mlx` is the "
            "tested recommendation)"))
    elif caps:
        pf.checks.append(Check("ok", "tool calling supported"))
    else:
        pf.checks.append(Check(
            "info", "tool support not reported by this server — if tool calls "
                    "fail, update ollama (`ollama -v` shows your version)"))
    if "vision" in caps:
        pf.vision = True
        pf.checks.append(Check("ok", "sees images — /paste or ctrl+v to attach"))

    # 4 · serving context — the silent-truncation footgun. Effective ctx, in
    # authority order: /api/ps context_length (the LOADED reality) → a num_ctx
    # model parameter → unknown (advice only, never a false verdict).
    effective: int | None = None
    ps = _get(root + "/api/ps") or {}
    for m in ps.get("models") or []:
        if isinstance(m, dict) and m.get("model") == model and m.get("context_length"):
            effective = int(m["context_length"])
    if effective is None and show:
        hit = re.search(r"^num_ctx\s+(\d+)", str(show.get("parameters") or ""), re.M)
        if hit:
            effective = int(hit.group(1))
    model_max = None
    for k, v in ((show or {}).get("model_info") or {}).items():
        if str(k).endswith(".context_length") and isinstance(v, int):
            model_max = v
    if effective is not None:
        pf.ctx_verified = True
        pf.context_window = effective
        if effective < CTX_FLOOR:
            pf.checks.append(Check(
                "fail", f"serving context is {effective:,} — ollama TRUNCATES "
                        "silently there and agent sessions die mid-task",
                _CTX_FIX))
        elif effective < CTX_GOOD:
            pf.checks.append(Check(
                "warn", f"serving context {effective:,} is tight for agent "
                        f"work — {CTX_RECOMMENDED:,} recommended", _CTX_FIX))
        else:
            pf.checks.append(Check("ok", f"serving context {effective:,} (verified)"))
    else:
        cap = f" (model max {model_max:,})" if model_max else ""
        pf.checks.append(Check(
            "warn", f"serving context can't be verified until the model "
                    f"loads{cap} — ollama's small default truncates SILENTLY",
            f"to be safe: {_CTX_FIX}"))
    return pf
