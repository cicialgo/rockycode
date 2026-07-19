"""User config — persistent preferences (currency, theme, language, …).

Resolution order (later wins): built-in DEFAULTS → global
`~/.rockycode/config.toml` → project `<wd>/.rockycode/config.toml`. CLI flags
and env vars still override at runtime; this file is just for sticky
preferences you don't want to retype.

TOML for reading (stdlib tomllib) to match the ecosystem; writes are a flat
hand-rolled emit (the config is flat, so no toml-writer dependency needed).
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Optional

GLOBAL_PATH = Path.home() / ".rockycode" / "config.toml"
PROJECT_REL = Path(".rockycode") / "config.toml"

DEFAULTS: dict[str, Any] = {
    "currency": "usd",      # usd | cny  — which official price table to show cost in
    "theme": "dark",        # dark | light
    # Reply language: auto mirrors whatever language the user writes in; zh
    # adds a native 语言要求 block to the prompt (reasoning + replies in
    # Chinese, code/identifiers untouched). Resolved once at session build.
    # UI strings are en for now; zh planned on this same key.
    "language": "auto",     # auto | en | zh
    "permission": "ask",    # yolo | ask | careful — tool-approval strictness
    # self-evolve is EXPERIMENTAL and ships default-OFF: nothing fires unless the
    # user opts in (set exit_sheet=auto|on and dream=auto). auto = only when the
    # dream pipeline is alive (memory on + Ollama reachable), so opting in still
    # never nags a user without the local stack.
    "exit_sheet": "off",    # off | auto | on — the exit feedback sheet (auto-skips after 60s)
    "dream": "manual",      # manual | auto — catch-up dream pass at launch (local Ollama)
    # Collaboration mode applied at launch ("" = none). Set by `/research always`.
    # Resolved against BUILT-IN modes only — a cloned repo's project config must
    # not auto-inject a project-local mode file into the prompt (trust rule,
    # same spirit as permission never loosening).
    "mode": "",
    # Model limits — set these for a non-DeepSeek model (env ROCKYCODE_CONTEXT_WINDOW
    # / ROCKYCODE_MAX_TOKENS and the CLI flags override at runtime).
    "context_window": 1_048_576,  # DeepSeek V4 = 1M; compaction acts at 50%
    "max_tokens": 384_000,        # per-call output cap incl. CoT (DeepSeek V4 max = 384K)
}

_ALLOWED = {
    "currency": {"usd", "cny"},
    "theme": {"dark", "light"},
    "language": {"auto", "en", "zh"},
    "permission": {"yolo", "ask", "careful"},
    "exit_sheet": {"auto", "on", "off"},
    "dream": {"auto", "manual"},
}


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


def _sanitize(raw: dict, base: dict) -> dict:
    """Merge *raw* onto *base*, dropping anything malformed.

    Config can come from an untrusted cloned repo, so a bad value must never
    crash the app or silently mean something dangerous. Unknown keys are
    ignored; numeric fields are coerced (a quoted TOML "7.2" arrives as str);
    enum fields must be in _ALLOWED or the base value is kept.
    """
    out = dict(base)
    for k, v in raw.items():
        if k not in DEFAULTS:
            continue
        default = DEFAULTS[k]
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            try:
                v = int(v) if isinstance(default, int) else float(v)
            except (TypeError, ValueError):
                continue
            if v <= 0:  # window / output must be positive
                continue
        if k in _ALLOWED and v not in _ALLOWED[k]:
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
        cfg = proj
    return cfg


def _coerce(key: str, raw: str) -> Any:
    default = DEFAULTS.get(key)
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, (int, float)) and not isinstance(default, bool):
        try:
            v = int(raw) if isinstance(default, int) else float(raw)
        except ValueError:
            return default
        return v if v > 0 else default
    return raw.strip()


def _emit(cfg: dict) -> str:
    lines = []
    for k, v in cfg.items():
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            lines.append(f"{k} = {v}")
        else:
            lines.append(f'{k} = "{v}"')
    return "\n".join(lines) + "\n"


def set_value(key: str, raw: str) -> tuple[Any, Optional[str]]:
    """Persist key=value to the GLOBAL config. Returns (value, error)."""
    if key not in DEFAULTS:
        return None, f"unknown key '{key}'. known: {', '.join(sorted(DEFAULTS))}"
    default = DEFAULTS[key]
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            value = int(raw)
        except ValueError:
            return None, f"{key} must be a positive integer (got {raw!r})"
        if value <= 0:
            return None, f"{key} must be positive (got {value})"
        GLOBAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        current = _read(GLOBAL_PATH)
        current[key] = value
        GLOBAL_PATH.write_text(_emit(current))
        return value, None
    value = _coerce(key, raw)
    if key in _ALLOWED and value not in _ALLOWED[key]:
        return None, f"invalid value for {key}: {value!r}. allowed: {', '.join(sorted(_ALLOWED[key]))}"
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
