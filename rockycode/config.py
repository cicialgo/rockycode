"""User config — persistent preferences (currency, theme, language, …).

Resolution order (later wins): built-in DEFAULTS → global
`~/.rockycode/config.toml` → project `<wd>/.rockycode/config.toml`. CLI flags
and env vars still override at runtime; this file is just for sticky
preferences you don't want to retype.

TOML for reading (stdlib tomllib) to match the ecosystem; writes preserve
nested tables such as [jev] using TOML inline tables.
"""
from __future__ import annotations

import json
import os
import re
import tomllib
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Optional
from rockycode.i18n import LOCALES

# $ROCKYCODE_HOME (tests and users who relocate the store) — same convention
# as session.py / trajectory.py / onboarding.py.
GLOBAL_PATH = Path(os.environ.get("ROCKYCODE_HOME") or Path.home() / ".rockycode") / "config.toml"
PROJECT_REL = Path(".rockycode") / "config.toml"

DEFAULTS: dict[str, Any] = {
    "currency": "usd",      # usd | cny  — which official price table to show cost in
    "theme": "dark",        # dark | light
    # Reply language: auto mirrors whatever language the user writes in; zh
    # adds a native 语言要求 block to the prompt (reasoning + replies in
    # Chinese, code/identifiers untouched). Resolved once at session build.
    "language": "auto",     # auto | en | zh — model response language
    "ui_language": "auto",  # auto | any rockycode/i18n/locales/<code>.toml — auto follows language, then English
    "permission": "ask",    # yolo | ask | careful — tool-approval strictness
    # Evolve v2 (locked 2026-08-02): reflection checkpoints on the session's
    # OWN model — warm-prefix calls at turn/compaction/exit boundaries, capped
    # per session — plus the launch catch-up dream that consolidates and
    # drafts proposals. Default OFF since 1.0.0: every evolve call bills the
    # session model, so it is opt-in (`/config evolve auto`); "off" kills every
    # evolve call. (The old exit_sheet/dream keys retired with Ollama — unknown
    # keys are ignored.)
    "evolve": "off",        # off | auto — reflect checkpoints + launch catch-up dream
    # Collaboration mode applied at launch ("" = none). Set by `/research always`.
    # Resolved against BUILT-IN modes only — a cloned repo's project config must
    # not auto-inject a project-local mode file into the prompt (trust rule,
    # same spirit as permission never loosening).
    "mode": "",
    # Launch model: a /model-style spec (model id, endpoint, or endpoint:model)
    # resolved against the provider registry at startup. "" = the old chain
    # (--model flag / ROCKYCODE_MODEL env). Scriptable — `rockycode config
    # model glm:flash` lets another rocky switch the default.
    # SECURITY: consumers read this (and vision_models) from the GLOBAL config
    # only — a cloned repo's project config must never redirect requests.
    "model": "",
    # Extra model ids to treat as image-capable (comma/space separated), merged
    # into the provider registry at load — for when a provider ships vision on
    # an EXISTING id (e.g. flash itself gains vision) before rocky's builtin
    # registry catches up. Unknown ids are ignored harmlessly.
    "vision_models": "",
    # Model limits. 0 = follow the active model's registry entry
    # (rockycode/models.toml → ~/.rockycode/models.toml override): context
    # window and per-call output cap come from the model, and change with a
    # /model switch. A positive value is YOUR ceiling and sticks across
    # switches (env ROCKYCODE_CONTEXT_WINDOW / ROCKYCODE_MAX_TOKENS and the
    # CLI flags override at runtime).
    "context_window": 0,
    "max_tokens": 0,
    # Image understanding on a NO-vision model (engine/vision.py), one key:
    #   auto  (default) a keyed vision endpoint describes the image silently —
    #         deepseek-flash on the home key, else the first keyed vision
    #         endpoint; none keyed → an honest placeholder, no picker in the way
    #   ask   the route picker at paste time (its "always" row writes auto here)
    #   off   placeholder only: the model is told an image exists
    #   <endpoint spec>  pin the describer, /model shape: kimi, kimi:kimi-k3
    #   <command>  your own image CLI: a known tool name ("mmx" — rocky knows
    #         its describe invocation, vision.KNOWN_CLIS) or a command template
    #         with {path} / optional {question}; without {path} the image is
    #         appended as the last argument.
    # A file written with the old image_route / image_provider / image_cli
    # keys is read as this one key (_legacy_image); the file is left alone.
    # SECURITY: a command here is a program rocky runs. It is read from the
    # GLOBAL config only (GLOBAL_ONLY) — a cloned repo must never choose it —
    # and typed by the user: the rocky_config tool refuses to set one.
    "image": "auto",
    # Server searches the native web_search backend may run inside ONE provider
    # request (Anthropic `max_uses`). 0 = no cap: the provider's own pause point
    # bounds a request and rocky's continuation cap bounds the turn. Providers bill
    # each server search (MiniMax: $0.01), so this is a spending knob as much as a
    # breadth knob; 12 keeps a deep research question broad. Read from the GLOBAL
    # config only — a cloned repo must not lift a billing cap.
    "search_max_uses": 12,
    # The look of an artifact rocky lays out itself (a fragment, not a full HTML
    # document): ink (default) | amber | lilac (rocky's purple) | none (type
    # only, no colours). Light and dark come with each; engine/artifact_page.py.
    "artifact_theme": "ink",
}

_ALLOWED = {
    "currency": {"usd", "cny"},
    "theme": {"dark", "light"},
    "language": {"auto", "en", "zh"},
    "ui_language": {"auto", *LOCALES},
    "permission": {"yolo", "ask", "careful"},
    "evolve": {"auto", "off"},
    "artifact_theme": {"ink", "amber", "lilac", "none"},
}
# Integer keys where 0 is meaningful ("follow the model"), not a bad value.
_ZERO_OK = {"context_window", "max_tokens", "search_max_uses"}
# Keys a project file cannot set: they redirect requests, run a program or spend
# money, so only the user's own global file decides them. load() enforces it for
# every reader; a project value for one of these is ignored.
GLOBAL_ONLY = ("model", "vision_models", "image", "search_max_uses")


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        return {}


# Tool-permission strictness, ranked. A project file may TIGHTEN the guard but
# never LOOSEN it (see load()).
_PERM_RANK = {"careful": 2, "ask": 1, "yolo": 0}


_LEGACY_IMAGE = ("image_route", "image_provider", "image_cli")


def _legacy_image(raw: dict) -> dict:
    """A config written before the one `image` key: fold the three old keys
    into it (the file itself is left alone). cli, and auto with a CLI set,
    become the command; provider becomes the pinned endpoint; ask and off
    stay; anything else is auto."""
    if "image" in raw or not any(k in raw for k in _LEGACY_IMAGE):
        return raw
    route = raw.get("image_route")
    cli = raw.get("image_cli") if isinstance(raw.get("image_cli"), str) else ""
    provider = raw.get("image_provider") if isinstance(raw.get("image_provider"), str) else ""
    if route in ("ask", "off"):
        image = route
    elif route == "cli":
        image = cli.strip() or "auto"
    elif route == "provider":
        image = provider.strip() or "auto"
    else:
        image = cli.strip() or provider.strip() or "auto"
    return {**raw, "image": image}


def _sanitize(raw: dict, base: dict) -> dict:
    """Merge *raw* onto *base*, dropping anything malformed.

    Config can come from an untrusted cloned repo, so a bad value must never
    crash the app or silently mean something dangerous. Unknown keys are
    ignored; numeric fields are coerced (a quoted TOML "7.2" arrives as str);
    string fields must be strings; enum fields must be in _ALLOWED or the base
    value is kept.
    """
    out = dict(base)
    for k, v in _legacy_image(raw).items():
        if k not in DEFAULTS:
            continue
        default = DEFAULTS[k]
        if isinstance(default, str) and not isinstance(v, str):
            continue
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            try:
                v = int(v) if isinstance(default, int) else float(v)
            except (TypeError, ValueError):
                continue
            if v < 0 or (v == 0 and k not in _ZERO_OK):  # window / output: positive, or 0 = auto
                continue
        if k in _ALLOWED and (not isinstance(v, str) or v not in _ALLOWED[k]):
            continue
        out[k] = v
    return out


def load(workdir: Optional[Path] = None) -> dict:
    cfg = _sanitize(_read(GLOBAL_PATH), DEFAULTS)
    if workdir is not None:
        proj = _sanitize(_read(workdir / PROJECT_REL), cfg)
        # Security: a project config (possibly from a cloned repo) may make the
        # tool-approval guard STRICTER but never weaker. Clamp any downgrade —
        # otherwise a hostile repo ships permission="yolo" and runs every tool
        # call unprompted. Tightening (ask→careful) is honored.
        if _PERM_RANK.get(proj.get("permission"), 1) < _PERM_RANK.get(cfg["permission"], 1):
            proj["permission"] = cfg["permission"]
        # Same rule for what redirects requests, runs a program or spends money: a
        # project file once chose the image command, which ran with no approval.
        for key in GLOBAL_ONLY:
            proj[key] = cfg[key]
        cfg = proj
    return cfg


def global_value(key: str) -> Any:
    """One key from the GLOBAL config only (project files cannot set it) —
    for knobs that spend the user's money or redirect requests."""
    return _sanitize(_read(GLOBAL_PATH), DEFAULTS)[key]


def describe_value(key: str, workdir: Optional[Path] = None) -> dict:
    """Describe an effective preference and its source, including project overrides."""
    if key not in DEFAULTS:
        raise KeyError(key)
    global_raw = _read(GLOBAL_PATH)
    global_cfg = _sanitize(global_raw, DEFAULTS)
    source = "default"
    path = None
    # A sentinel base distinguishes an explicitly valid value from a malformed one.
    sentinel = object()
    if _sanitize(global_raw, {key: sentinel}).get(key, sentinel) is not sentinel:
        source, path = "global", GLOBAL_PATH
    global_only = key in GLOBAL_ONLY
    effective = global_cfg if global_only else load(workdir)
    if workdir is not None and not global_only:
        project_path = Path(workdir) / PROJECT_REL
        project = _sanitize(_read(project_path), {key: sentinel})
        if project.get(key, sentinel) is not sentinel and project[key] == effective[key]:
            source, path = "project", project_path
    return {"value": effective[key], "source": source, "path": path,
            "global_value": global_cfg[key]}


def change_feedback(key: str, workdir: Optional[Path] = None, locale: str = "en") -> str:
    """Explain saved-global versus effective-project values and session boundaries."""
    from rockycode.i18n import tr
    details = describe_value(key, workdir)
    notes = []
    # These preferences are deliberately consumed from global config only.
    if details["source"] == "project" and key not in GLOBAL_ONLY:
        notes.append(tr("config.project_override", locale, setting=key,
                        value=details["value"], path=details["path"]))
    if key in ("language", "ui_language"):
        notes.append(tr("config.restart", locale))
    return " ".join(notes)


def _coerce(key: str, raw: str) -> Any:
    default = DEFAULTS.get(key)
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, (int, float)) and not isinstance(default, bool):
        try:
            v = int(raw) if isinstance(default, int) else float(raw)
        except ValueError:
            return default
        return v if v > 0 or (v == 0 and key in _ZERO_OK) else default
    raw = raw.strip()
    # Users quote values out of shell habit (/config image "mmx …") — the
    # quotes are not part of the value, and stored literally they would both
    # break shlex-split consumers and corrupt the emitted TOML.
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1]
    return raw


def _emit(cfg: dict) -> str:
    def quote(s: str) -> str:
        # JSON's raw DEL is illegal in a TOML basic string. Keep non-ASCII
        # literal (JSON surrogate-pair escapes are not valid TOML scalars).
        return json.dumps(s, ensure_ascii=False).replace("\x7f", "\\u007f")

    def key(k: str) -> str:
        return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else quote(k)

    def value(v) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, str):
            return quote(v)
        if isinstance(v, dict):
            return "{ " + ", ".join(f"{key(k)} = {value(item)}" for k, item in v.items()) + " }"
        if isinstance(v, list):
            return "[" + ", ".join(value(item) for item in v) + "]"
        if isinstance(v, (datetime, date, time)):
            return v.isoformat()
        raise TypeError(f"unsupported TOML value: {type(v).__name__}")

    return "\n".join(f"{key(k)} = {value(v)}" for k, v in cfg.items()) + "\n"


def set_value(key: str, raw: str, *, locale: str = "en") -> tuple[Any, Optional[str]]:
    """Persist key=value to the GLOBAL config. Returns (value, error)."""
    from rockycode.i18n import tr
    if key not in DEFAULTS:
        return None, tr("config.unknown_key", locale, setting=key, known=", ".join(sorted(DEFAULTS)))
    default = DEFAULTS[key]
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            value = int(raw)
        except ValueError:
            return None, tr("config.positive_integer", locale, setting=key, value=raw)
        if value < 0 or (value == 0 and key not in _ZERO_OK):
            return None, tr("config.positive", locale, setting=key, value=value)
        GLOBAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        current = _read(GLOBAL_PATH)
        current[key] = value
        GLOBAL_PATH.write_text(_emit(current))
        return value, None
    value = _coerce(key, raw)
    if key in _ALLOWED and value not in _ALLOWED[key]:
        return None, tr("config.invalid_value", locale, setting=key, value=value,
                        allowed=", ".join(sorted(_ALLOWED[key])))
    GLOBAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    current = _read(GLOBAL_PATH)
    current[key] = value
    GLOBAL_PATH.write_text(_emit(current))
    return value, None


def set_project_value(workdir: Path, key: str, raw: str) -> tuple[Any, Optional[str]]:
    """Persist key=value to THIS folder's .rockycode/config.toml — folder-scoped
    defaults like `/research always`. Same validation as set_value."""
    if key not in DEFAULTS:
        return None, f"unknown key '{key}'. known: {', '.join(sorted(DEFAULTS))}"
    value = _coerce(key, raw)
    if key in _ALLOWED and value not in _ALLOWED[key]:
        return None, f"invalid value for {key}: {value!r}. allowed: {', '.join(sorted(_ALLOWED[key]))}"
    path = Path(workdir) / PROJECT_REL
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        current = _read(path)
        current[key] = value
        path.write_text(_emit(current))
    except OSError as e:
        return None, f"could not write {path}: {e}"
    return value, None
