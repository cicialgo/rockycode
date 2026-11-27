"""The key file and the first run: placeholder-aware key detection, a saver that
keeps every other line (0600, atomic), and the first-run flow for any provider,
a local server or an own URL. The prompts are scripted; nothing touches the
network, the real key store or the OS keychain."""
import os
import re
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

home = Path(tempfile.mkdtemp(prefix="rocky-onboarding-"))
os.environ["ROCKYCODE_HOME"] = str(home)
os.environ["ROCKYCODE_OLLAMA_URL"] = "http://127.0.0.1:9"  # nothing listens there
for _name in [n for n in os.environ if re.fullmatch(r"ROCKYCODE_(\w+_)?(API_KEY|BASE_URL)|ROCKYCODE_MODEL|"
                                                    r"OPENAI_API_KEY|ANTHROPIC_AUTH_TOKEN", n)]:
    os.environ.pop(_name)

from rich.console import Console  # noqa: E402

from rockycode import onboarding  # noqa: E402
from rockycode.cli import firstrun  # noqa: E402
from rockycode.engine import providers as P  # noqa: E402
from rockycode.engine import route as R  # noqa: E402

onboarding._keyring_key = lambda: None  # hermetic — never read the real OS keychain
onboarding._keyring = lambda: None      # and never offer to write to it
ENV = onboarding.GLOBAL_ENV
assert ENV == home / ".env", ENV


def names(path=ENV):
    return [line.split("=")[0] for line in path.read_text().splitlines()]


def forget(*keep):
    """Back to a machine where rocky has nothing but the lines of its key file."""
    for name in [n for n in os.environ if n.startswith("ROCKYCODE_") and n.endswith(("_API_KEY", "_BASE_URL"))
                 or n == "ROCKYCODE_MODEL"]:
        if name not in keep:
            os.environ.pop(name)


# key detection: nothing set → unset; a template placeholder → still unset
assert onboarding.current_key() is None and not onboarding.is_configured()
os.environ["ROCKYCODE_API_KEY"] = "sk-replace-me"
assert onboarding.current_key() is None, "placeholder must count as unset (triggers setup)"
os.environ["ROCKYCODE_API_KEY"] = "sk-realkey123"
assert onboarding.current_key() == "sk-realkey123" and onboarding.is_configured()
forget()
print("key detection: placeholder-aware  ✓")

# save_env: the lines it was asked to write, and every other line untouched
ENV.write_text("# my notes\nROCKYCODE_KIMI_API_KEY=k-kimi\nexport ROCKYCODE_OLLAMA_URL=http://box:11434\n"
               "ROCKYCODE_MODEL=old\nROCKYCODE_MODEL=older-duplicate\n")
onboarding.save_env({"ROCKYCODE_MODEL": "kimi:kimi-k3", "ROCKYCODE_QWEN_PLAN_API_KEY": "k plan #1"})
assert ENV.read_text().splitlines() == [
    "# my notes", "ROCKYCODE_KIMI_API_KEY=k-kimi", "export ROCKYCODE_OLLAMA_URL=http://box:11434",
    "ROCKYCODE_MODEL=kimi:kimi-k3", 'ROCKYCODE_QWEN_PLAN_API_KEY="k plan #1"'], ENV.read_text()
assert stat.S_IMODE(ENV.stat().st_mode) == 0o600, oct(ENV.stat().st_mode)
assert stat.S_IMODE(ENV.parent.stat().st_mode) == 0o700
assert [p.name for p in home.iterdir() if p.name.startswith(".env")] == [".env"], "no temp file left behind"
from dotenv import dotenv_values  # noqa: E402
assert dotenv_values(ENV)["ROCKYCODE_QWEN_PLAN_API_KEY"] == "k plan #1", "a quoted value reads back whole"
onboarding.save_env({"ROCKYCODE_QWEN_PLAN_API_KEY": None})
assert "ROCKYCODE_QWEN_PLAN_API_KEY" not in names() and "ROCKYCODE_KIMI_API_KEY" in names()
try:
    onboarding.save_env({"ROCKYCODE_MODEL": "two\nlines"})
except ValueError:
    pass
else:
    raise AssertionError("a value must not be able to add lines to the key file")
# A key file the user keeps elsewhere and links here stays a link; one that
# cannot be read is an error, never treated as empty and replaced.
vault = home / "vault" / "rocky.env"
vault.parent.mkdir()
vault.write_text("ROCKYCODE_GLM_API_KEY=k-glm\n")
linked = home / "linked" / ".env"
linked.parent.mkdir()
linked.symlink_to(vault)
onboarding.save_env({"ROCKYCODE_MODEL": "glm:glm-5.3"}, linked)
assert linked.is_symlink() and vault.read_text().splitlines() == ["ROCKYCODE_GLM_API_KEY=k-glm",
                                                                    "ROCKYCODE_MODEL=glm:glm-5.3"]
vault.write_bytes(b"ROCKYCODE_GLM_API_KEY=k-glm\n\xff\xfe not text\n")
try:
    onboarding.save_env({"ROCKYCODE_MODEL": "x"}, vault)
except UnicodeDecodeError:
    assert vault.read_bytes().startswith(b"ROCKYCODE_GLM_API_KEY=k-glm"), "left exactly as it was"
else:
    raise AssertionError("an unreadable key file must not be replaced")
assert [p.name for p in vault.parent.iterdir()] == ["rocky.env"], "and no temp copy of the keys stays behind"
print("save_env: writes its own lines, keeps the rest, 0600 from the first byte, atomic  ✓")
print("save_env: follows a linked key file, refuses to replace one it cannot read  ✓")

# The bug this replaces: saving the home key rewrote the whole file.
onboarding.save_credentials("sk-abc", "https://api.deepseek.com/v1", "deepseek-flash")
content = ENV.read_text()
assert "ROCKYCODE_API_KEY=sk-abc" in content and "ROCKYCODE_MODEL=deepseek-flash" in content, content
assert "ROCKYCODE_BASE_URL=https://api.deepseek.com/v1" in content and "OPENAI_BASE_URL" not in content
assert "ROCKYCODE_KIMI_API_KEY=k-kimi" in content and "# my notes" in content, \
    "another provider's key and the user's own lines survive"
assert os.environ["ROCKYCODE_API_KEY"] == "sk-abc" and onboarding.is_configured()
print("save_credentials: the home key lands without costing the user any other line  ✓")


class Script:
    """Scripted answers for the flow's prompts; records what was asked and how."""

    def __init__(self, *answers, verdicts=("ok",)):
        self.answers, self.asked, self.verdicts, self.probed = list(answers), [], list(verdicts), []

    def ask(self, text, *, default=None, hide=False):
        self.asked.append((text.strip(), hide))
        answer = self.answers.pop(0)
        return default if answer is None else answer

    def probe(self, key, base_url):
        self.probed.append(base_url)
        return self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]

    def run(self, missing=None):
        with patch.object(firstrun, "_ask", self.ask), patch.object(firstrun, "probe_key", self.probe):
            firstrun.run_setup(Console(file=open(os.devnull, "w")), missing)
        assert not self.answers, f"unasked answers left: {self.answers}"
        return self


def number(eid):
    return str(1 + [r.endpoint.eid for r in firstrun.rows()].index(eid))


OWN_URL = str(len(firstrun.rows()) + 1)
assert [r.endpoint.eid for r in firstrun.rows()][0] == "deepseek", "the default answer stays DeepSeek"
labels = [r.label for r in firstrun.rows()]
assert "Qwen (Model Studio / 百炼) · Bailian Token Plan" in labels and any("Ollama" in x for x in labels), labels

# First run with a Kimi key and nothing else: no DeepSeek key is asked for.
ENV.unlink()
forget()
flow = Script(number("kimi"), "k-kimi-new", "1", None).run()
assert [hide for text, hide in flow.asked if "key" in text] == [True], "a key is always typed hidden"
assert flow.probed == [P.catalog()["kimi"].endpoints[0].base_url], "tested against Kimi's own endpoint"
assert names() == ["ROCKYCODE_KIMI_API_KEY", "ROCKYCODE_MODEL"], names()
assert "ROCKYCODE_MODEL=kimi:kimi-k3" in ENV.read_text()
assert R.route(os.environ["ROCKYCODE_MODEL"]).key == "k-kimi-new", "and the session can start"
print("first run: any provider — a Kimi key, a Kimi default model, no DeepSeek anything  ✓")

# A named model that only lacks its key: that one key, nothing else, nothing lost.
try:
    R.route("qwen-plan:qwen3.8-max")
except R.RouteError as error:
    missing = error
flow = Script("k-rejected", "k-plan", verdicts=("rejected", "ok")).run(missing)
assert len(flow.asked) == 2 and all(hide for _, hide in flow.asked), flow.asked
assert names() == ["ROCKYCODE_KIMI_API_KEY", "ROCKYCODE_MODEL", "ROCKYCODE_QWEN_PLAN_API_KEY"], names()
assert "ROCKYCODE_MODEL=kimi:kimi-k3" in ENV.read_text(), "the default model is not touched"
assert R.route("qwen-plan:qwen3.8-max").key == "k-plan"
print("first run: a model missing its key asks for that key only, and a rejected paste is asked again  ✓")

# Where there is an OS keychain, the first run offers it for any key.
ENV.unlink()
forget()
stored = {}
keyring = NS(set_password=lambda service, user, value: stored.update({(service, user): value}),
             get_password=lambda service, user: stored.get((service, user)),
             delete_password=lambda service, user: stored.pop((service, user)))
with patch.object(onboarding, "_keyring", lambda: keyring), patch.object(firstrun, "_yes", lambda *a, **k: True):
    Script(number("deepseek"), "k-home", "1", "zh").run()
    assert stored == {("rockycode", "ROCKYCODE_API_KEY"): "k-home"}
    assert names() == ["ROCKYCODE_KEYCHAIN", "ROCKYCODE_MODEL"], "the key is kept out of the file; only its name is listed"
    assert os.environ["ROCKYCODE_API_KEY"] == "k-home" and onboarding.current_key_source() == "keychain"
    Script(number("minimax"), "k-minimax", "1", None).run()
    assert stored[("rockycode", "ROCKYCODE_MINIMAX_API_KEY")] == "k-minimax", "a provider key goes there too"
    assert names() == ["ROCKYCODE_KEYCHAIN", "ROCKYCODE_MODEL"] and "k-minimax" not in ENV.read_text()
    assert "ROCKYCODE_KEYCHAIN=ROCKYCODE_API_KEY,ROCKYCODE_MINIMAX_API_KEY" in ENV.read_text()
    with patch.object(firstrun, "_yes", lambda *a, **k: False):  # asked, and the answer is no
        Script(number("kimi"), "k-kimi-file", "1", None).run()
    assert ("rockycode", "ROCKYCODE_KIMI_API_KEY") not in stored and "ROCKYCODE_KIMI_API_KEY=k-kimi-file" in ENV.read_text()
    # Yes, but the keychain will not take it: nothing is saved, and the user is asked again.
    unlocked = keyring.set_password
    keyring.set_password = lambda *a: (_ for _ in ()).throw(RuntimeError("the keychain is locked"))
    locked = Script(number("qwen"), "k-qwen", "1", "2", "1", None)  # try again (still locked), then the key file
    seen, plain_ask = [], locked.ask

    def watching(text, **kw):
        seen.append((text.strip(), "k-qwen" in ENV.read_text(), os.environ.get("ROCKYCODE_QWEN_API_KEY")))
        return plain_ask(text, **kw)

    locked.ask = watching
    locked.run()
    asked_again = [(in_file, in_process) for text, in_file, in_process in seen[2:4]]
    assert asked_again == [(False, None), (False, None)], f"a refused key is nowhere until the user picks: {seen}"
    assert "ROCKYCODE_QWEN_API_KEY=k-qwen" in ENV.read_text(), "the key file, once it is the user's pick"
    assert ("rockycode", "ROCKYCODE_QWEN_API_KEY") not in stored
    # Unlocked in between: "try again" lands it in the keychain, and the file never sees it.
    attempts = []

    def unlocks_later(service, user, value):
        attempts.append(user)
        if len(attempts) == 1:
            raise RuntimeError("the keychain is locked")
        unlocked(service, user, value)

    keyring.set_password = unlocks_later
    Script(number("glm"), "k-glm", "1", "1", None).run()
    assert stored[("rockycode", "ROCKYCODE_GLM_API_KEY")] == "k-glm" and "k-glm" not in ENV.read_text()
    assert attempts == ["ROCKYCODE_GLM_API_KEY"] * 2
print("first run: the keychain is offered for any key; a no means the 0600 file; a keychain that refuses "
      "saves nothing and asks again  ✓")

# A local server that is down is said so, and the menu comes back.
ENV.unlink()
forget()
flow = Script(number("ollama"), OWN_URL, "https://gateway.example/v1", "k-gateway", "my-model", None).run()
assert names() == ["ROCKYCODE_API_KEY", "ROCKYCODE_BASE_URL", "ROCKYCODE_MODEL"], names()
assert flow.probed == ["https://gateway.example/v1"]
home_route = R.route("my-model")
assert (home_route.base_url, home_route.key, home_route.via) == ("https://gateway.example/v1", "k-gateway", "home")
assert R.gateway(), "an own URL is the user's gateway: it may serve any model"
print("first run: a local server that is down returns to the menu; an own URL becomes the home endpoint  ✓")

# setup probe: an unreachable endpoint must never block setup (offline ≠ invalid)
assert firstrun.probe_key("sk-any", "http://127.0.0.1:1/v1") == "unverified"
print("setup probe: unreachable endpoint never blocks (can't verify != invalid)  ✓")

print("ONBOARDING SMOKE OK — any provider, one key, nothing lost. amaze!")
