"""First-run setup — the paste-your-key flow — and the credential chain.

A brand-new user who runs `rockycode` with no API key shouldn't hit a stack
trace. If nothing is configured we walk them through pasting a key once and save
it to ~/.rockycode/.env (a 0600 file that every run loads), so from then on
`rockycode` just works in any folder — no shell exports to remember.

Rocky reads exactly ONE key name: ROCKYCODE_API_KEY (env or the opt-in OS
keychain). It never picks up an ambient OPENAI_API_KEY — that usually belongs
to a DIFFERENT provider, and silently sending it to a DeepSeek endpoint would
ship a real OpenAI credential to a third party. Installs that stored the key
under the old OPENAI_API_KEY name (in rocky's OWN .env / keychain entry, where
it is rocky's key by construction) are renamed in place by
bootstrap_credentials().

The key lands in a file, never asked to be exported into the shell env by hand;
rocky's SDK clients receive it explicitly (require_key()) instead of trawling
the process env, and rocky's redaction keeps it out of tool output /
trajectories.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv

# The one key name rocky reads — rocky-owned, collides with nothing.
KEY_ENV = "ROCKYCODE_API_KEY"
# Never read: it usually belongs to a different provider. Mentioned in
# messages/warnings only.
_LEGACY_KEY_ENV = "OPENAI_API_KEY"
# The one endpoint name rocky reads. The SDK's ambient OPENAI_BASE_URL is
# never consulted — whoever controls the URL receives the Authorization
# header, so the endpoint gets the same protection as the key itself.
BASE_URL_ENV = "ROCKYCODE_BASE_URL"
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"

# $ROCKYCODE_HOME (tests and users who relocate the store) — same convention
# as session.py / trajectory.py.
_HOME = Path(os.environ.get("ROCKYCODE_HOME") or Path.home() / ".rockycode")
GLOBAL_ENV = _HOME / ".env"
_PLACEHOLDERS = {"", "replace-me", "sk-replace-me", "your-api-key", "your_api_key", "changeme"}

# Optional OS keychain (the `rockycode[keyring]` extra) — encrypted at rest, the
# safest place for the secret. macOS Keychain / Windows Credential Manager /
# Linux Secret Service. When absent we fall back to the 0600 dotenv.
_KEYRING_SERVICE = "rockycode"
_KEYRING_USER = KEY_ENV


def _keyring():
    """The keyring module IF the extra is installed AND has a real backend (not
    the fail/null backend on headless Linux); else None."""
    try:
        import keyring
        from keyring.backends.fail import Keyring as _FailBackend
        if isinstance(keyring.get_keyring(), _FailBackend):
            return None
        return keyring
    except Exception:  # noqa: BLE001 — optional; any import/backend issue → unavailable
        return None


def _keyring_key() -> str | None:
    kr = _keyring()
    if kr is None:
        return None
    try:
        return (kr.get_password(_KEYRING_SERVICE, _KEYRING_USER) or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


def _env_key() -> str | None:
    """ROCKYCODE_API_KEY from the env — placeholder-aware, so a template
    line pasted without editing still counts as unset (and triggers setup)."""
    v = (os.getenv(KEY_ENV) or "").strip()
    if v and v.lower() not in _PLACEHOLDERS:
        return v
    return None


def current_key() -> str | None:
    """The configured API key, or None."""
    v = _env_key()
    if v:
        return v
    v = _keyring_key()  # opt-in OS keychain
    if v and v.lower() not in _PLACEHOLDERS:
        return v
    return None


# Set once by bootstrap_credentials()/save_credentials(): "shell export",
# the global .env's display path, or "keychain". Recorded at fill time because
# afterwards everything looks like an env var (the keychain is copied into the
# process env) — post-hoc inspection can't tell the sources apart.
_KEY_SOURCE: str | None = None


def _global_env_display() -> str:
    return str(GLOBAL_ENV).replace(str(Path.home()), "~")


def current_key_source() -> str | None:
    """Where the key ACTUALLY came from — "shell export", the global .env's
    path, or "keychain". Surfaced in auth-error diagnostics so "which key is
    rocky using?" never needs guesswork. Falls back to coarse detection when
    bootstrap hasn't run (direct API/library use)."""
    if current_key() is None:
        return None
    if _KEY_SOURCE is not None:
        return _KEY_SOURCE
    if _env_key():
        return KEY_ENV
    return "keychain"


def is_configured() -> bool:
    return current_key() is not None


def require_key() -> str:
    """The configured key, or a RuntimeError that says how to fix it. SDK
    clients call this instead of letting the SDK trawl env for OPENAI_API_KEY —
    that fallback is exactly the misrouting this module exists to prevent."""
    key = current_key()
    if key:
        return key
    raise RuntimeError(
        f"no API key configured — run `rockycode` once to set one up, or set {KEY_ENV}. "
        f"(rocky reads only {KEY_ENV}; an ambient {_LEGACY_KEY_ENV} is never used — "
        "it usually belongs to a different provider)"
    )


def require_base_url() -> str:
    """The endpoint rocky talks to: {BASE_URL_ENV} (shell export or rocky's
    global .env) with the DeepSeek default. Clients pass this explicitly so
    the SDK never falls back to an ambient OPENAI_BASE_URL — and a project
    folder can never redirect the key (see project_env_warnings)."""
    v = (os.getenv(BASE_URL_ENV) or "").strip()
    return v or DEFAULT_BASE_URL


def provider_key(key_env: str) -> str:
    """A named provider's key from its ROCKY-OWNED env var (loaded from
    ~/.rockycode by bootstrap). NEVER an ambient provider key: a user's own
    MINIMAX_API_KEY is theirs; rocky reads only ROCKYCODE_<PROVIDER>_API_KEY.
    Raises with the exact var to set if it's missing."""
    if key_env == KEY_ENV:  # the default provider — full keychain chain
        return require_key()
    v = (os.getenv(key_env) or "").strip()
    if v and v.lower() not in _PLACEHOLDERS:
        return v
    raise RuntimeError(
        f"no key for this provider — set {key_env} in ~/.rockycode/.env "
        f"(or export it). rocky reads only rocky-owned {key_env}, never an "
        f"ambient provider key."
    )


# Credential-shaped names in a PROJECT .env — matched by name only, values
# are never read out. \w*_BASE_URL catches OPENAI_/ROCKYCODE_/any proxy vars.
_CREDENTIAL_SHAPED = re.compile(
    r"^\s*(?:export\s+)?((?:\w+_)?API_KEY|\w+_BASE_URL|\w+_AUTH_TOKEN)\s*=",
    re.MULTILINE,
)


def project_env_warnings(workdir: Path) -> list[str]:
    """One warning line per credential-shaped var NAME in the project's .env.

    Rocky never loads project .env files (a repo must not be able to supply a
    key or redirect the endpoint), but silence made that indistinguishable
    from "it worked" — a stale key shadowing the real one cost an evening.
    Names only; values never appear in the output."""
    try:
        content = (workdir / ".env").read_text()
    except OSError:
        return []
    names = sorted(set(_CREDENTIAL_SHAPED.findall(content)))
    return [
        f"project .env sets {name} — ignored. rocky reads credentials and "
        f"endpoint only from ~/.rockycode (or shell {KEY_ENV} / {BASE_URL_ENV})"
        for name in names
    ]


def load_credentials_into_env() -> None:
    """Load a keychain-stored key into the process env so every entry point
    resolves the same chain. No-op if a key is already set or the keyring extra
    isn't installed."""
    if _env_key():
        return
    v = _keyring_key()
    if v:
        os.environ[KEY_ENV] = v


def bootstrap_credentials() -> None:
    """CLI startup: load rocky's global .env, then fill from the keychain.
    Shell exports win (load_dotenv never overrides existing vars); project
    folders are never read. Records where the key came from — attribution is
    only knowable at fill time. Idempotent (first run wins the attribution)."""
    global _KEY_SOURCE
    first = _KEY_SOURCE is None
    if first and _env_key():
        _KEY_SOURCE = "shell export"
    load_dotenv(GLOBAL_ENV)
    if first and _KEY_SOURCE is None and _env_key():
        _KEY_SOURCE = _global_env_display()
    load_credentials_into_env()
    if first and _KEY_SOURCE is None and _env_key():
        _KEY_SOURCE = "keychain"


def save_credentials(key: str, base_url: str, model: str, path: Path = GLOBAL_ENV,
                     *, prefer_keyring: bool = False) -> Path:
    """Persist creds and apply to the running process. With prefer_keyring (and
    the [keyring] extra installed) the secret goes to the OS keychain and is kept
    OUT of the file; otherwise it lands in a 0600 dotenv. Non-secret settings
    always go to the .env. Dir is 0700, file 0600 — defense in depth."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)  # ~/.rockycode: user-only
    except OSError:
        pass

    in_keyring = False
    if prefer_keyring:
        kr = _keyring()
        if kr is not None:
            try:
                kr.set_password(_KEYRING_SERVICE, _KEYRING_USER, key)
                in_keyring = True
            except Exception:  # noqa: BLE001 — fall back to the file
                in_keyring = False

    lines = [f"{BASE_URL_ENV}={base_url}", f"ROCKYCODE_MODEL={model}"]
    if not in_keyring:
        lines.insert(0, f"{KEY_ENV}={key}")
    content = ("\n".join(lines) + "\n").encode("utf-8")
    # Create with 0600 from the start — a write-then-chmod leaves the secret
    # briefly group/other-readable (umask 0644) on a shared host.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content)
    finally:
        os.close(fd)
    try:
        path.chmod(0o600)  # tighten an existing file (O_CREAT keeps old perms)
    except OSError:
        pass

    os.environ[KEY_ENV] = key
    os.environ.setdefault(BASE_URL_ENV, base_url)
    os.environ.setdefault("ROCKYCODE_MODEL", model)
    global _KEY_SOURCE
    _KEY_SOURCE = "keychain" if in_keyring else _global_env_display()
    return path


def _key_rejected(key: str, base_url: str) -> bool:
    """True only when the endpoint definitively rejects the key (HTTP 401),
    via the cheapest authenticated call there is (GET /models). Unreachable
    endpoint / any other failure → False: never block setup when we can't
    verify (offline, odd proxy, provider without /models). Would have caught
    a paste with three stray blanks the moment it was entered."""
    from openai import AuthenticationError, OpenAI
    try:
        OpenAI(api_key=key, base_url=base_url, max_retries=0, timeout=10.0).models.list()
    except AuthenticationError:
        return True
    except Exception:  # noqa: BLE001 — can't verify ≠ invalid
        return False
    return False


def run_setup(console) -> None:
    """Interactive first-run. No-op once a key is configured."""
    if is_configured():
        return
    import typer

    from rockycode.banner import info, show_banner

    show_banner(console)
    console.print()
    info(console, "first run — let's set up your API key (saved to ~/.rockycode/.env, not your shell).")
    console.print(
        "  get a DeepSeek key: [cyan]https://platform.deepseek.com/api_keys[/]  "
        "·  any OpenAI-compatible key works too"
    )
    console.print()
    key = typer.prompt("  paste your API key", hide_input=True).strip()
    while not key:
        key = typer.prompt("  a key is required — paste it", hide_input=True).strip()
    base_url = typer.prompt("  API base URL", default="https://api.deepseek.com/v1").strip()
    while _key_rejected(key, base_url):
        info(console, "that key was rejected by the endpoint (401) — check for typos or stray spaces.")
        key = typer.prompt("  paste your API key again", hide_input=True).strip()
    model = typer.prompt("  default model (deepseek-v4-flash is the cheaper, lighter tier)",
                         default="deepseek-v4-pro").strip()
    # Reply language — asked once so a 中文 user's first session already
    # answers in Chinese instead of depending on model luck. auto mirrors
    # whatever language each message is written in. Changeable anytime:
    # `rockycode config language zh` or /config in the TUI.
    lang = typer.prompt(
        "  reply language / 回复语言 (auto = follow my messages · en · zh)",
        default="auto").strip().lower()
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
    prefer_keyring = False
    if _keyring() is not None:
        prefer_keyring = typer.confirm(
            "  store the key in your OS keychain (encrypted — safer than a file)?", default=True)
    save_credentials(key, base_url, model, prefer_keyring=prefer_keyring)
    where = "your OS keychain" if (prefer_keyring and _keyring_key()) else str(GLOBAL_ENV)
    info(console, f"key saved to {where} — you're set. `rockycode` works in any project now.")
    console.print()
