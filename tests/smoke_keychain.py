"""Keys in the OS keychain — any key, not only the home key.

A key saved to the keychain is its own entry there (service "rockycode", the
variable's name) and leaves the key file, which then lists only the NAME. A
start reads back exactly the listed names and nothing else; a shell export
still wins; a keychain that gives nothing is said so instead of reading as a
key never added; and the keys already in the key file can be moved over, each
read back before its line goes. A keychain that does not take a key saves
nothing (the plain-text file is chosen, never fallen into), and only the
operating system's own store is called "your OS keychain".

Hermetic: a throwaway home, a dict standing in for the keychain. The real OS
keychain is never opened — a relocated store never gets one, and the last
section drives the real `keyring` package only over an in-memory backend.
"""
import os
import re
import stat
import tempfile
from pathlib import Path

home = Path(tempfile.mkdtemp(prefix="rocky-keychain-"))
os.environ["ROCKYCODE_HOME"] = str(home)
os.environ["ROCKYCODE_OLLAMA_URL"] = "http://127.0.0.1:9"  # nothing listens there
for _name in [n for n in os.environ if re.fullmatch(r"ROCKYCODE_(\w+_)?(API_KEY|BASE_URL)|ROCKYCODE_MODEL|"
                                                    r"ROCKYCODE_KEYCHAIN", n)]:
    os.environ.pop(_name)

from rockycode import onboarding  # noqa: E402
from rockycode.engine import route as R  # noqa: E402

ENV = onboarding.GLOBAL_ENV
assert ENV == home / ".env", ENV
HOME, KIMI, MINIMAX, QWEN = ("ROCKYCODE_API_KEY", "ROCKYCODE_KIMI_API_KEY", "ROCKYCODE_MINIMAX_API_KEY",
                             "ROCKYCODE_QWEN_API_KEY")
real_keyring = onboarding._keyring


def lines():
    return ENV.read_text().splitlines()


# ── a relocated store never gets the OS keychain, whatever is installed ──────
assert onboarding._relocated() and onboarding._keyring() is None and not onboarding.keychain_available()
where = onboarding.save_key(KIMI, "k-kimi-file", prefer_keyring=True)
assert where.endswith(".env") and lines() == [f"{KIMI}=k-kimi-file"], (where, lines())
print("relocated: $ROCKYCODE_HOME keeps its keys in its own file — a test can never reach the real keychain  ✓")


class FakeKeychain:
    """keyring's three calls over a dict. locked = every call raises;
    forgets = accepts a value and drops it (a null backend); refuses = names it will not take."""

    def __init__(self):
        self.items, self.calls, self.locked, self.forgets, self.refuses = {}, [], False, False, set()

    def get_password(self, service, name):
        self.calls.append(("get", name))
        if self.locked:
            raise RuntimeError("the keychain is locked")
        return self.items.get((service, name))

    def set_password(self, service, name, value):
        self.calls.append(("set", name))
        if self.locked or name in self.refuses:
            raise RuntimeError("the keychain is locked")
        if not self.forgets:
            self.items[(service, name)] = value

    def delete_password(self, service, name):
        self.calls.append(("delete", name))
        if (service, name) not in self.items:
            raise RuntimeError("no such entry")
        del self.items[(service, name)]


chain = FakeKeychain()
onboarding._keyring = lambda: chain


def fresh_start(**exports):
    """A new process: only the given shell exports in the env, nothing remembered."""
    for name in [n for n in os.environ if n.startswith("ROCKYCODE_")
                 and n not in ("ROCKYCODE_HOME", "ROCKYCODE_OLLAMA_URL")]:
        os.environ.pop(name)
    os.environ.update(exports)
    onboarding._KEY_SOURCE, onboarding._OLDER_HOME_ASKED = None, False
    for memo in (onboarding._STORE_NAMES, onboarding._KEYCHAIN_LOADED, onboarding._KEYCHAIN_SILENT):
        memo.clear()
    chain.calls.clear()
    onboarding.bootstrap_credentials()


# ── any key goes to the keychain; the file keeps its name, never its value ───
ENV.write_text(f"# my notes\nROCKYCODE_MODEL=kimi:kimi-k3\n{KIMI}=k-kimi-file\n")
assert onboarding.keychain_available()
assert onboarding.save_key(KIMI, "k-kimi", prefer_keyring=True) == onboarding.KEYCHAIN
assert lines() == ["# my notes", "ROCKYCODE_MODEL=kimi:kimi-k3", f"ROCKYCODE_KEYCHAIN={KIMI}"], lines()
assert chain.items == {("rockycode", KIMI): "k-kimi"} and os.environ[KIMI] == "k-kimi"
assert onboarding.key_place(KIMI) == "keychain" and stat.S_IMODE(ENV.stat().st_mode) == 0o600
assert onboarding.save_key(HOME, "k-home", prefer_keyring=True) == onboarding.KEYCHAIN
assert lines()[-1] == f"ROCKYCODE_KEYCHAIN={KIMI},{HOME}" and onboarding.current_key_source() == "keychain"
assert "k-kimi" not in ENV.read_text() and "k-home" not in ENV.read_text()
print("save: a provider key and the home key each get their own keychain entry; the file lists names only  ✓")

# ── a start reads back the listed names, and only those ─────────────────────
fresh_start()
assert os.environ[KIMI] == "k-kimi" and os.environ[HOME] == "k-home"
assert chain.calls == [("get", KIMI), ("get", HOME)], chain.calls
assert onboarding.key_place(KIMI) == onboarding.key_place(HOME) == "keychain"
assert onboarding.current_key_source() == "keychain" and not onboarding.keychain_silent()
assert R.route("kimi:kimi-k3").key == "k-kimi", "and the session can start"
shell = onboarding.tool_env()
assert not {KIMI, HOME, "ROCKYCODE_KEYCHAIN"} & set(shell), "what the keychain gave stays in rocky's own process"
fresh_start(**{KIMI: "k-shell"})
assert os.environ[KIMI] == "k-shell" and onboarding.key_place(KIMI) == "shell", "a shell export still wins"
assert chain.calls == [("get", HOME)], "and the keychain is not asked for a name already set"
print("start: one read per listed key; a shell export wins; none of it reaches the shell tool  ✓")

# ── nothing listed, a key in the file: the keychain is never opened ─────────
ENV.write_text(f"{HOME}=k-file-home\n")
fresh_start()
assert chain.calls == [] and onboarding.key_place(HOME) == "file", chain.calls
# an install from before the list existed: the home key sits in the keychain, unlisted
ENV.write_text("ROCKYCODE_MODEL=deepseek:deepseek-flash\n")
fresh_start()
assert os.environ[HOME] == "k-home" and onboarding.current_key_source() == "keychain"
assert chain.calls == [("get", HOME)], chain.calls
print("start: a store with no keychain keys opens nothing; an older keychain home key is still found  ✓")

# ── back to the file: one key, one home ─────────────────────────────────────
ENV.write_text(f"ROCKYCODE_KEYCHAIN={KIMI},{HOME}\n")
fresh_start()
assert onboarding.save_key(KIMI, "k-kimi-2").endswith(".env")
assert lines() == [f"ROCKYCODE_KEYCHAIN={HOME}", f"{KIMI}=k-kimi-2"], lines()
assert ("rockycode", KIMI) not in chain.items and onboarding.key_place(KIMI) == "file"
assert onboarding.save_key(HOME, "k-home-2").endswith(".env")
assert lines() == [f"{KIMI}=k-kimi-2", f"{HOME}=k-home-2"] and not chain.items, (lines(), chain.items)
print("file: a key saved to the file leaves the keychain, and the last one takes the list with it  ✓")

# ── a keychain that will not take the key saves nothing ─────────────────────
for trouble in ("locked", "forgets"):
    setattr(chain, trouble, True)
    before = ENV.read_text()
    try:
        onboarding.save_key(QWEN, f"k-qwen-{trouble}", prefer_keyring=True)
    except onboarding.KeychainRefused as refused:
        assert f"k-qwen-{trouble}" not in str(refused), "the refusal names the key, never its value"
    else:
        raise AssertionError("a keychain that did not take the key must not read as saved")
    assert ENV.read_text() == before and QWEN not in os.environ, "not in the file, not in this process"
    assert onboarding.key_place(QWEN) == ""
    setattr(chain, trouble, False)
# the plain-text file is a choice the user makes, in so many words
assert onboarding.save_key(QWEN, "k-qwen-file").endswith(".env")
assert f"{QWEN}=k-qwen-file" in lines() and "ROCKYCODE_KEYCHAIN" not in ENV.read_text()
assert os.environ[QWEN] == "k-qwen-file" and onboarding.key_place(QWEN) == "file"
print("refused: a locked keychain, or one that accepts and forgets, saves nothing; the file is chosen, never fallen into  ✓")

# ── a keychain that gives nothing at start is said so ───────────────────────
chain.items.clear()
ENV.write_text("")
onboarding.save_key(KIMI, "k-kimi-3", prefer_keyring=True)
chain.locked = True
fresh_start()
assert KIMI not in os.environ and onboarding.keychain_silent() == [KIMI]
try:
    R.route("kimi:kimi-k3")
    raise AssertionError("a silent keychain must not produce a route")
except R.RouteError as error:
    assert error.key_env == KIMI and "listed as kept in the OS keychain, which gave nothing" in str(error), str(error)
    assert "k-kimi-3" not in str(error)
assert onboarding.keychain_note(MINIMAX) == "", "a key that was never added keeps the plain message"
# a long-lived process builds many runtimes: the keychain is asked once, not once per task
chain.calls.clear()
onboarding.bootstrap_credentials()
onboarding.bootstrap_credentials()
assert ("get", KIMI) not in chain.calls and chain.calls.count(("get", HOME)) <= 1, chain.calls
chain.locked = False
onboarding.bootstrap_credentials()
assert KIMI not in os.environ, "still this process: unlock, then restart"
fresh_start()
assert os.environ[KIMI] == "k-kimi-3" and not onboarding.keychain_silent()
print("silent: a locked keychain reads as \"the keychain gave nothing\", not as a key never added  ✓")

# ── move the keys already in the file ───────────────────────────────────────
chain.items.clear()
ENV.write_text(f"# my notes\n{HOME}=k-a\n{MINIMAX}=k-m\nROCKYCODE_MODEL=minimax:m3\n{QWEN}=sk-replace-me\n")
fresh_start()
assert onboarding.keys_in_file() == [HOME, MINIMAX], "a placeholder is not a key; a setting is not a key"
chain.refuses = {MINIMAX}
assert onboarding.move_to_keychain() == ([HOME], [MINIMAX])
assert lines() == ["# my notes", f"{MINIMAX}=k-m", "ROCKYCODE_MODEL=minimax:m3", f"{QWEN}=sk-replace-me",
                   f"ROCKYCODE_KEYCHAIN={HOME}"], lines()
assert onboarding.key_place(HOME) == "keychain" and onboarding.key_place(MINIMAX) == "file"
chain.refuses = set()
assert onboarding.move_to_keychain() == ([MINIMAX], [])
assert "k-a" not in ENV.read_text() and "k-m" not in ENV.read_text()
assert lines()[0] == "# my notes" and lines()[-1] == f"ROCKYCODE_KEYCHAIN={HOME},{MINIMAX}"
assert chain.items == {("rockycode", HOME): "k-a", ("rockycode", MINIMAX): "k-m"}
assert onboarding.move_to_keychain() == ([], []), "nothing left to move"
fresh_start()
assert os.environ[HOME] == "k-a" and os.environ[MINIMAX] == "k-m"
print("move: each key is read back before its line leaves the file; one the keychain refuses stays put  ✓")

# ── only the operating system's own store is "your OS keychain" ─────────────
def backend(module, members=None):
    """A stand-in with a backend's module name; members makes it a chain."""
    made = type("Keyring", (), {"__module__": module})()
    if members is not None:
        made.backends = members
    return made


for real in ("keyring.backends.macOS", "keyring.backends.Windows", "keyring.backends.SecretService",
             "keyring.backends.libsecret", "keyring.backends.kwallet"):
    assert onboarding._is_os_keychain(backend(real)), real
for other in ("keyrings.alt.file", "keyring.backends.null", "keyring.backends.fail", "some_plugin.vault", "__main__"):
    assert not onboarding._is_os_keychain(backend(other)), f"{other} is not the OS keychain"
chained = "keyring.backends.chainer"
assert onboarding._is_os_keychain(backend(chained, [backend("keyring.backends.SecretService"),
                                                    backend("keyring.backends.kwallet")]))
assert not onboarding._is_os_keychain(backend(chained, [backend("keyring.backends.SecretService"),
                                                        backend("keyrings.alt.file")])), \
    "a chain reads from every member: one plain-text file in it and the label would be a lie"
assert not onboarding._is_os_keychain(backend(chained, [])), "an empty chain stores nothing"
print("label: a plain-text or third-party backend is not offered as the OS keychain, alone or in a chain  ✓")

# ── the real `keyring` package, over an in-memory backend ───────────────────
try:
    import keyring
    from keyring.backend import KeyringBackend
    from keyring.backends.null import Keyring as NullKeyring
    from keyring.errors import PasswordDeleteError
except ImportError:
    print("keyring: the package is not installed here — its own API is driven where it is  (skipped)")
else:
    class Memory(KeyringBackend):
        priority = 1

        def __init__(self):
            super().__init__()
            self.items = {}

        def get_password(self, service, username):
            return self.items.get((service, username))

        def set_password(self, service, username, password):
            self.items[(service, username)] = password

        def delete_password(self, service, username):
            if (service, username) not in self.items:
                raise PasswordDeleteError("not found")
            del self.items[(service, username)]

    memory = Memory()
    keyring.set_keyring(memory)  # set before anything asks: the OS backend is never built
    onboarding._keyring, was_relocated = real_keyring, onboarding._relocated
    onboarding._relocated = lambda: False  # as the default home would be — over the memory backend only
    try:
        ENV.write_text("")
        # Through the real package too: a backend rocky does not know as an OS store is no keychain,
        # so the key goes to rocky's own file under its own name, not into that backend.
        assert not onboarding.keychain_available()
        assert onboarding.save_key(KIMI, "k-unlisted", prefer_keyring=True).endswith(".env") and not memory.items
        onboarding._OS_KEYCHAINS += (Memory.__module__,)  # from here on it stands in for the OS store
        ENV.write_text("")
        assert onboarding.keychain_available()
        assert onboarding.save_key(KIMI, "k-real-api", prefer_keyring=True) == onboarding.KEYCHAIN
        assert memory.items == {("rockycode", KIMI): "k-real-api"} and lines() == [f"ROCKYCODE_KEYCHAIN={KIMI}"]
        os.environ.pop(KIMI)
        onboarding.load_credentials_into_env()
        assert os.environ[KIMI] == "k-real-api"
        assert onboarding.save_key(KIMI, "k-real-file").endswith(".env") and not memory.items
        onboarding._drop_from_keychain(KIMI)  # an entry that is not there: no error
        keyring.set_keyring(NullKeyring())
        assert not onboarding.keychain_available(), "a backend that stores nothing is no keychain"
        keyring.set_keyring(memory)
        onboarding._relocated = was_relocated
        assert not onboarding.keychain_available(), "and a relocated store has none, whatever the backend"
        # a harness that sets $ROCKYCODE_HOME after importing rocky is covered too
        relocated_at_import, onboarding._RELOCATED = onboarding._RELOCATED, False
        assert onboarding._relocated() and not onboarding.keychain_available()
        onboarding._RELOCATED = relocated_at_import
    finally:
        onboarding._relocated = was_relocated
    from importlib.metadata import version
    print(f"keyring: the real package ({version('keyring')}) saves, reads and drops through rocky's calls  ✓")

print("KEYCHAIN SMOKE OK — every key has one home, and the safer one comes first. amaze!")
