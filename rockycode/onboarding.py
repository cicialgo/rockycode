"""The credential chain: where rocky's keys live, and how they are saved.

Keys live in the OS keychain when there is one (each key its own entry; the
key file then lists only its NAME) or in ~/.rockycode/.env (a 0600 file that
every run loads) — so `rockycode` works in any folder with no shell exports to
remember. Whoever saves a key says which: the first run (cli/firstrun.py), the
TUI's key field, the desktop's key form. All of them save through save_key(),
and a key is saved only where the user said: a keychain that does not take it
raises KeychainRefused, and the plain-text file is a choice they make again.

Rocky reads only rocky-OWNED names: ROCKYCODE_API_KEY for the home endpoint,
ROCKYCODE_<PROVIDER>_API_KEY for every other. It never picks up an ambient
OPENAI_API_KEY — that usually belongs to a DIFFERENT provider, and silently
sending it to another endpoint would ship a real credential to a third party.
Installs that stored the key under the old OPENAI_API_KEY name (in rocky's OWN
.env / keychain entry, where it is rocky's key by construction) are renamed in
place by bootstrap_credentials().

Rocky's SDK clients receive their key explicitly (engine/route.py decides
which key goes to which endpoint) instead of trawling the process env, and
rocky's redaction keeps it out of tool output / trajectories. What the store
loads stays in rocky's own process: tool_env() is the environment handed to
the commands and servers rocky starts.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

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
_RELOCATED = bool(os.environ.get("ROCKYCODE_HOME"))
_HOME = Path(os.environ.get("ROCKYCODE_HOME") or Path.home() / ".rockycode")
GLOBAL_ENV = _HOME / ".env"
_PLACEHOLDERS = {"", "replace-me", "sk-replace-me", "your-api-key", "your_api_key", "changeme"}

# The OS keychain — encrypted at rest, and not a file a command can read: macOS
# Keychain / Windows Credential Manager / Linux Secret Service, through the
# `keyring` package. Each key is its own entry (service "rockycode", account =
# the variable's name). The key file keeps ONE line of names, never values —
# which keys to ask the keychain for — so a start that uses none never opens it.
# Where there is no keychain the 0600 key file holds the keys, as before.
_KEYRING_SERVICE = "rockycode"
_KEYRING_USER = KEY_ENV
KEYCHAIN_ENV = "ROCKYCODE_KEYCHAIN"
KEYCHAIN = "your OS keychain"  # save_key()'s "where" for a key that went there


def _relocated() -> bool:
    """Is the store somewhere other than ~/.rockycode — when this module was
    imported, or now (a test harness sets $ROCKYCODE_HOME after its imports)?"""
    return _RELOCATED or bool(os.environ.get("ROCKYCODE_HOME"))


# The backends that ARE the operating system's own encrypted store. `keyring`
# also loads whatever else is installed (keyrings.alt's plain-text file, a
# third-party plugin), and rocky must not call that "your OS keychain": the
# label is a promise about where the key sits. Anything else counts as no
# keychain, and the key goes to rocky's own 0600 file under its own name.
_OS_KEYCHAINS = ("keyring.backends.macOS", "keyring.backends.Windows", "keyring.backends.SecretService",
                 "keyring.backends.libsecret", "keyring.backends.kwallet")


def _is_os_keychain(backend) -> bool:
    module = type(backend).__module__
    if module == "keyring.backends.chainer":
        # A chain reads from every member and writes to the first: all of them must qualify.
        members = list(getattr(backend, "backends", None) or ())
        return bool(members) and all(_is_os_keychain(member) for member in members)
    return any(module == name or module.startswith(name + ".") for name in _OS_KEYCHAINS)


def _keyring():
    """The keyring module IF it is installed AND its backend is the operating
    system's own store (_OS_KEYCHAINS — not the fail/null backend of a headless
    box, not a file); else None. A relocated store ($ROCKYCODE_HOME: a second
    profile, a container, a test) never gets it: the keychain has one
    "rockycode", and it belongs to the default home."""
    if _relocated():
        return None
    try:
        import keyring
        if not _is_os_keychain(keyring.get_keyring()):
            return None
        return keyring
    except Exception:  # noqa: BLE001 — optional; any import/backend issue → unavailable
        return None


def keychain_available() -> bool:
    return _keyring() is not None


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


def probe_key(key: str, base_url: str) -> str:
    """Test the key with the cheapest authenticated call there is (GET
    /models — free, no tokens billed). Returns:
      "ok"         — the endpoint accepted the key
      "rejected"   — definitive HTTP 401: the key is wrong
      "unverified" — endpoint unreachable / no /models route: can't tell.
    Only "rejected" blocks setup — offline or odd-proxy users still proceed.
    Would have caught a paste with three stray blanks the moment it was entered."""
    from openai import AuthenticationError, OpenAI
    try:
        OpenAI(api_key=key, base_url=base_url, max_retries=0, timeout=10.0).models.list()
    except AuthenticationError:
        return "rejected"
    except Exception:  # noqa: BLE001 — can't verify ≠ invalid
        return "unverified"
    return "ok"


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


NO_KEY = (
    f"no API key configured — run `rockycode` once to set one up, or set {KEY_ENV}. "
    f"(rocky reads only {KEY_ENV}; an ambient {_LEGACY_KEY_ENV} is never used — "
    "it usually belongs to a different provider)"
)


def require_key() -> str:
    """The home key, or a RuntimeError that says how to fix it. Never the SDK's
    own trawl of the env for OPENAI_API_KEY — that fallback is exactly the
    misrouting this module exists to prevent."""
    key = current_key()
    if key:
        return key
    raise RuntimeError(NO_KEY)


def require_base_url() -> str:
    """The endpoint rocky talks to: {BASE_URL_ENV} (shell export or rocky's
    global .env) with the DeepSeek default. Clients pass this explicitly so
    the SDK never falls back to an ambient OPENAI_BASE_URL — and a project
    folder can never redirect the key (see project_env_warnings)."""
    v = (os.getenv(BASE_URL_ENV) or "").strip()
    return v or DEFAULT_BASE_URL


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


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Recorded at fill time, like _KEY_SOURCE: afterwards a value is just a variable.
_KEYCHAIN_LOADED: set[str] = set()  # names this process took from the keychain
_KEYCHAIN_SILENT: set[str] = set()  # listed in the key file, but the keychain gave nothing
_OLDER_HOME_ASKED = False           # the unlisted home key of an older install: looked for once


def keychain_names(path: Path = GLOBAL_ENV) -> list[str]:
    """The names the key file lists as kept in the OS keychain. Names only."""
    try:
        listed = dotenv_values(path).get(KEYCHAIN_ENV) or ""
    except OSError:
        return []
    return [name for name in re.split(r"[,\s]+", listed) if _NAME.fullmatch(name)]


def keychain_silent() -> list[str]:
    """Keys the key file says are in the OS keychain, which did not give them at
    start: a locked keychain, a remote or scheduled session, a new machine."""
    return sorted(_KEYCHAIN_SILENT)


def keychain_note(name: str) -> str:
    """One sentence for a key the OS keychain should hold and did not give —
    appended to the "no key" message, so a locked keychain does not read as a
    key that was never added."""
    if name not in _KEYCHAIN_SILENT:
        return ""
    return (f" {name} is listed as kept in the OS keychain, which gave nothing: it may be locked, or "
            "this session (ssh, a scheduled job) cannot open it. Unlock it, or add the key again.")


def _usable(value: str | None) -> bool:
    return bool(value and value.strip() and value.strip().lower() not in _PLACEHOLDERS)


def load_credentials_into_env() -> None:
    """Fill the process env from the OS keychain so every entry point resolves
    the same chain: each name the key file lists there, and the home key of an
    install from before the list existed. A name already set (a shell export,
    the key file) is left alone; a start with nothing to fetch opens nothing.

    This runs again for every runtime a long-lived process builds (a desktop
    task, an MCP call). Asking can be an OS dialog, so a keychain that gave
    nothing for a name is asked once per process, not once per task: unlock
    it, then restart (the desktop's "Check again" starts a new process)."""
    global _OLDER_HOME_ASKED
    listed = keychain_names()
    wanted = [name for name in listed if not _usable(os.getenv(name)) and name not in _KEYCHAIN_SILENT]
    if wanted:
        kr = _keyring()
        for name in wanted:
            try:
                value = kr.get_password(_KEYRING_SERVICE, name) if kr is not None else None
            except Exception:  # noqa: BLE001 — locked, denied, no session: the same "no answer"
                value = None
            if _usable(value):
                os.environ[name] = value.strip()
                _KEYCHAIN_LOADED.add(name)
            else:
                _KEYCHAIN_SILENT.add(name)
    if KEY_ENV not in listed and not _env_key() and not _OLDER_HOME_ASKED:
        _OLDER_HOME_ASKED = True
        v = _keyring_key()  # a home key saved to the keychain before the list existed
        if v:
            os.environ[KEY_ENV] = v
            _KEYCHAIN_LOADED.add(KEY_ENV)


def key_place(name: str) -> str:
    """Where the key this process uses under *name* came from: "keychain",
    "file" (rocky's key file), "shell" (an export), or "" when there is none.
    For labels — never the value."""
    if not _usable(os.getenv(name)):
        return ""
    if name in _KEYCHAIN_LOADED:
        return "keychain"
    return "file" if name in _STORE_NAMES else "shell"


# Names rocky's OWN store (the global .env, the keychain, first-run setup) put
# into this process's environment. Recorded at fill time for the same reason as
# _KEY_SOURCE: afterwards a stored value looks exactly like a shell export.
_STORE_NAMES: set[str] = set()
# rocky's own credential variables, whatever put them there (a shell export
# included). ROCKYCODE_MAX_TOKENS / ROCKYCODE_TOKEN_BUDGET are settings, not these.
_OWN_SECRET = re.compile(r"^ROCKYCODE_(\w+_)?(API_KEY|TOKEN|SECRET|PASSWORD)$", re.I)


def tool_env() -> dict[str, str]:
    """The environment for a process rocky starts in the workspace on the
    model's behalf — the shell tool, the code checks, MCP and LSP servers.

    It is the user's own environment minus everything rocky loaded from its
    store and minus rocky's own credential variables. Redaction already keeps
    a key out of tool OUTPUT, but a command that can read a key can still use
    it (`curl -d "$ROCKYCODE_API_KEY" …`), so the key must not be there at all.
    The user's own exports (GITHUB_TOKEN, their project's OPENAI_API_KEY) pass
    through untouched: a coding agent's shell has to run their project. A
    nested `rockycode` loads the store itself."""
    return {k: v for k, v in os.environ.items()
            if k not in _STORE_NAMES and not _OWN_SECRET.match(k)}


def bootstrap_credentials() -> None:
    """CLI startup: load rocky's global .env, then fill from the keychain.
    Shell exports win (load_dotenv never overrides existing vars); project
    folders are never read. Records where the key came from — attribution is
    only knowable at fill time. Idempotent (first run wins the attribution)."""
    global _KEY_SOURCE
    first = _KEY_SOURCE is None
    if first and _env_key():
        _KEY_SOURCE = "shell export"
    before = set(os.environ)
    load_dotenv(GLOBAL_ENV)
    if first and _KEY_SOURCE is None and _env_key():
        _KEY_SOURCE = _global_env_display()
    load_credentials_into_env()
    _STORE_NAMES.update(set(os.environ) - before)
    if first and _KEY_SOURCE is None and _env_key():
        _KEY_SOURCE = "keychain"


_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _env_value(value: str) -> str:
    """A dotenv value: bare when it can be, double-quoted when a space, `#` or
    quote would otherwise end or change it."""
    if "\n" in value or "\r" in value:
        raise ValueError("a stored value cannot span lines")
    if not value or any(c in value for c in " \t#'\"\\"):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def save_env(values: dict[str, str | None], path: Path = GLOBAL_ENV) -> Path:
    """Write NAME=value lines into rocky's .env, keeping every other line of
    the file as it is; a None value removes that name. Saving one provider's
    key must never cost the user another's (a rewrite of the whole file once
    did). Dir 0700, file 0600 from its first byte, swapped in atomically — a
    crash mid-write leaves the old file, never half of one. A file that exists
    but cannot be read is an error, not an empty file: guessing would erase it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)  # ~/.rockycode: user-only
    except OSError:
        pass
    if path.is_symlink():
        path = Path(os.path.realpath(path))  # keep a link into the user's own secret store a link
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    kept: list[str] = []
    written: set[str] = set()
    for line in lines:
        hit = _ENV_LINE.match(line)
        name = hit.group(1) if hit else None
        if name not in values:
            kept.append(line)
        elif values[name] is not None and name not in written:
            kept.append(f"{name}={_env_value(values[name])}")
            written.add(name)
    kept += [f"{name}={_env_value(value)}" for name, value in values.items()
             if value is not None and name not in written]
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    # Created 0600: a write-then-chmod would leave the secret briefly
    # group/other-readable (umask 0644) on a shared host.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        try:
            os.write(fd, "".join(line + "\n" for line in kept).encode("utf-8"))
        finally:
            os.close(fd)
        try:
            tmp.chmod(0o600)  # a leftover temp file keeps its old mode through O_CREAT
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)  # never leave a second copy of the keys behind
        raise
    return path


def _apply(name: str, value: str, *, override: bool) -> None:
    """A saved value takes effect in the running process too. A setting yields
    to a shell export (override=False); a key just entered replaces what is there."""
    before = set(os.environ)
    if override:
        os.environ[name] = value
    else:
        os.environ.setdefault(name, value)
    _STORE_NAMES.update(set(os.environ) - before)


def _to_keychain(name: str, key: str) -> bool:
    """Put one key in the OS keychain. True only when it reads back: a backend
    that accepts a value and forgets it must not cost the user the key."""
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.set_password(_KEYRING_SERVICE, name, key)
        return kr.get_password(_KEYRING_SERVICE, name) == key
    except Exception:  # noqa: BLE001 — locked, denied, no session: the keychain did not take it
        return False


def _drop_from_keychain(name: str) -> None:
    kr = _keyring()
    try:
        if kr is not None:
            kr.delete_password(_KEYRING_SERVICE, name)
    except Exception:  # noqa: BLE001 — no such entry; or one that will not go, which is no longer listed
        pass


def _placed(names: list[str], path: Path, *, keychain: bool) -> None:
    """Record where rocky's store now holds these keys, for labels and for tool_env()."""
    for name in names:
        (_KEYCHAIN_LOADED.add if keychain else _KEYCHAIN_LOADED.discard)(name)
        _KEYCHAIN_SILENT.discard(name)
        _STORE_NAMES.add(name)
    if KEY_ENV in names:
        global _KEY_SOURCE
        _KEY_SOURCE = "keychain" if keychain else str(path).replace(str(Path.home()), "~")


class KeychainRefused(Exception):
    """The OS keychain was asked to keep a key and did not: it is locked, this
    session cannot open it, or it took the value and did not give it back.
    Nothing was saved anywhere. The caller asks the user what to do next."""


def save_key(key_env: str, key: str, path: Path = GLOBAL_ENV, *, prefer_keyring: bool = False) -> str:
    """Persist one key under its rocky-owned name and apply it to the running
    process. Returns where it went, for the message. With prefer_keyring and an
    OS keychain on this machine the key goes there and is kept OUT of the file,
    which then lists only its name; a keychain that does not take it raises
    KeychainRefused and nothing is saved — the user chose the keychain, so a
    plain-text copy is theirs to choose as well. Without prefer_keyring, or
    where there is no keychain at all, it lands in the 0600 dotenv, and a copy
    the keychain held is dropped: one key has one home."""
    listed = keychain_names(path)
    in_keyring = prefer_keyring and keychain_available()
    if in_keyring and not _to_keychain(key_env, key):
        raise KeychainRefused(key_env)
    names = [name for name in listed if name != key_env] + ([key_env] if in_keyring else [])
    save_env({key_env: None if in_keyring else key, KEYCHAIN_ENV: ",".join(names) or None}, path)
    if not in_keyring and (key_env in listed or key_env == KEY_ENV):
        _drop_from_keychain(key_env)
    _apply(key_env, key, override=True)
    _placed([key_env], path, keychain=in_keyring)
    return KEYCHAIN if in_keyring else str(path).replace(str(Path.home()), "~")


def keys_in_file(path: Path = GLOBAL_ENV) -> list[str]:
    """Names of rocky's own keys that sit in the key file as plain text."""
    try:
        values = dotenv_values(path)
    except OSError:
        return []
    return [name for name, value in values.items() if _OWN_SECRET.match(name) and _usable(value)]


def move_to_keychain(path: Path = GLOBAL_ENV) -> tuple[list[str], list[str]]:
    """Move every rocky key in the key file into the OS keychain. Each is read
    back before its line leaves the file; one the keychain does not take stays
    where it is. Returns (names moved, names left in the file)."""
    try:
        values = dotenv_values(path)
    except OSError:
        return [], []
    found = keys_in_file(path)
    moved = [name for name in found if _to_keychain(name, values[name].strip())]
    if moved:
        names = [name for name in keychain_names(path) if name not in moved] + moved
        save_env({**dict.fromkeys(moved), KEYCHAIN_ENV: ",".join(names)}, path)
        # A name the shell exports with another value keeps that value, and its "shell" label.
        _placed([name for name in moved if os.getenv(name, "").strip() == values[name].strip()],
                path, keychain=True)
    return moved, [name for name in found if name not in moved]


def save_setting(name: str, value: str, path: Path = GLOBAL_ENV) -> None:
    """Persist a non-secret launch setting (the default model, the home URL)."""
    save_env({name: value}, path)
    _apply(name, value, override=False)


def save_credentials(key: str, base_url: str, model: str, path: Path = GLOBAL_ENV,
                     *, prefer_keyring: bool = False) -> Path:
    """The home endpoint in one call: its key, its URL and the default model."""
    save_key(KEY_ENV, key, path, prefer_keyring=prefer_keyring)
    save_setting(BASE_URL_ENV, base_url, path)
    save_setting("ROCKYCODE_MODEL", model, path)
    return path
