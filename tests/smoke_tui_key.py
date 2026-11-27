"""Add or change a key inside the app (headless pilot, no network, a throwaway home).

`/model` on a model whose provider has no key opens a hidden field instead of
printing an env-file recipe: the key is tested, saved by the first run's own
saver (0600, every other line kept), applied to the running process, and the
switch goes through. A key that exists is changed from the `/model` picker's
"add or change a key" row: pick the endpoint, the same field opens.

Where the machine has an OS keychain, the field's first row saves there (the
key file then lists only the key's name), the list says where each key lives,
and its last row moves the keys still in the file.

The key must stay inside that field: not in the transcript, not in the chat
input's history, not in the session record. There is no `/key` command — its
name invited a key typed right after it, and a typed line is echoed and kept.
"""
import asyncio
import os
import re
import stat
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
home = Path(tempfile.mkdtemp(prefix="rocky-key-home-"))
os.environ["ROCKYCODE_HOME"] = str(home)
os.environ["ROCKYCODE_OLLAMA_URL"] = "http://127.0.0.1:9"
for _name in [n for n in os.environ if re.fullmatch(r"ROCKYCODE_(\w+_)?(API_KEY|BASE_URL)|ROCKYCODE_MODEL", n)]:
    os.environ.pop(_name)
os.chdir(tempfile.mkdtemp(prefix="rocky-key-work-"))

from textual.widgets import Input, OptionList, Static  # noqa: E402

from rockycode import onboarding  # noqa: E402

onboarding._keyring_key = lambda: None  # hermetic — never read the real OS keychain
onboarding._keyring = lambda: None      # and never write to it

from rockycode.engine import providers as P  # noqa: E402
from rockycode.engine.loop import Engine  # noqa: E402
from rockycode.tui import commands  # noqa: E402
from rockycode.tui.app import RockyCodeApp  # noqa: E402
from rockycode.tui.chatinput import ChatInput  # noqa: E402
from rockycode.tui.keyprompt import KeyPicker, KeyScreen  # noqa: E402
from rockycode.tui.modelpicker import ModelPicker  # noqa: E402
from rockycode.tui.prompt_history import PromptHistory  # noqa: E402

ENV = onboarding.GLOBAL_ENV
assert ENV == home / ".env", ENV
ENV.write_text("# my notes\nROCKYCODE_API_KEY=k-home-fixture\n")
os.environ["ROCKYCODE_API_KEY"] = "k-home-fixture"
SECRET = "k-kimi-SECRET-do-not-leak"
KIMI = P.catalog()["kimi"].endpoints[0]


def transcript(app) -> str:
    return "\n".join(str(w.render()) for w in app._transcript().query(Static))


async def settle(pilot, condition, tries=80):
    for _ in range(tries):
        await pilot.pause(0.05)
        # A screen is on top before its on_mount has filled it: wait for both.
        if pilot.app.screen.is_mounted and condition():
            return
    raise AssertionError("the screen did not reach the expected state")


async def main():
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace()))
    engine = Engine(model="deepseek-flash", client=client, workdir=Path.cwd())
    engine.evolve_enabled = False
    app = RockyCodeApp(engine, permission="yolo", prompt_history=PromptHistory())  # on disk, as the real CLI
    probed = []
    verdicts = ["rejected", "ok"]

    def probe(key, base_url):
        probed.append((key, base_url))
        return verdicts.pop(0) if len(verdicts) > 1 else verdicts[0]

    with patch.object(onboarding, "probe_key", probe):
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause()

            # ── /model on a model with no key: the field, not a recipe ────────────
            await commands.dispatch(app, "/model kimi-k3")
            await settle(pilot, lambda: isinstance(app.screen, KeyScreen))
            field = app.screen.query_one("#key-input", Input)
            assert field.password, "the key is typed hidden"
            assert app.focused is field
            body = str(app.screen.query_one("#key-body", Static).render())
            assert "Kimi" in body and "never goes into the chat or to a model" in body, body
            assert engine.model == "deepseek-flash", "nothing switches before a key exists"

            # empty → asked again; a rejected key → said so, field cleared, still here
            await pilot.press("enter")
            await pilot.pause(0.1)
            assert "a key is needed" in str(app.screen.query_one("#key-status", Static).render())
            field.value = "k-typo"
            await pilot.press("enter")
            await settle(pilot, lambda: "rejected" in str(app.screen.query_one("#key-status", Static).render()))
            assert field.value == "" and isinstance(app.screen, KeyScreen) and not field.disabled
            assert "ROCKYCODE_KIMI_API_KEY" not in ENV.read_text(), "a rejected key is never saved"

            # the right key → saved, applied, and the switch that asked for it goes through
            field.value = f"  {SECRET}  "
            await pilot.press("enter")
            await settle(pilot, lambda: not isinstance(app.screen, KeyScreen) and engine.model == "kimi-k3")
            assert probed == [("k-typo", KIMI.base_url), (SECRET, KIMI.base_url)], "tested on Kimi's own endpoint"
            saved = ENV.read_text().splitlines()
            assert saved == ["# my notes", "ROCKYCODE_API_KEY=k-home-fixture", f"ROCKYCODE_KIMI_API_KEY={SECRET}"], saved
            assert stat.S_IMODE(ENV.stat().st_mode) == 0o600
            assert os.environ["ROCKYCODE_KIMI_API_KEY"] == SECRET
            assert engine.provider_name == "kimi" and engine.route.key == SECRET and engine.route.base_url == KIMI.base_url
            text = transcript(app)
            assert "key saved to" in text and "ROCKYCODE_KIMI_API_KEY" in text, text
            assert app.focused is app.query_one(ChatInput), "and typing goes on"
            print("model: a model with no key asks for it in a hidden field, tests it, saves it, then switches  ✓")

            # ── a key that exists: /model → "add or change a key" ─────────────────
            async def open_key_list():
                await commands.dispatch(app, "/model")
                # The screen is up one step before its list is filled: an empty list has no last row.
                await settle(pilot, lambda: isinstance(app.screen, ModelPicker)
                             and app.screen.query_one("#model-list", OptionList).option_count > 0)
                models = app.screen.query_one("#model-list", OptionList)
                last = models.option_count - 1
                assert models.get_option_at_index(last).id == "keys"
                assert "add or change a key" in str(models.get_option_at_index(last).prompt)
                models.highlighted = last
                await pilot.pause(0.1)
                assert "hidden field" in str(app.screen.query_one("#model-preview", Static).render())
                models.action_select()
                await settle(pilot, lambda: isinstance(app.screen, KeyPicker))
                return app.screen.query_one("#keys-list", OptionList)

            rows = await open_key_list()
            listed = [str(rows.get_option_at_index(i).prompt) for i in range(rows.option_count)]
            here = listed[rows.highlighted]  # opens ON the endpoint in use (kimi now)
            assert here.startswith("▸ ") and "Kimi" in here and "key in the key file" in here, here
            assert not any("move the keys" in row for row in listed), "no keychain here: nothing to move to"
            assert any("no key yet" in row for row in listed), listed
            assert not any("ollama" in row.lower() for row in listed), "a local server takes no key"
            rows.action_select()
            await settle(pilot, lambda: isinstance(app.screen, KeyScreen))
            assert app.screen.key_env == "ROCKYCODE_KIMI_API_KEY"
            app.screen.query_one("#key-input", Input).value = "k-kimi-second"
            actions = app.screen.query_one("#key-actions", OptionList)
            assert [actions.get_option_at_index(i).id for i in range(actions.option_count)] == ["file", "cancel"]
            actions.highlighted = 0
            actions.action_select()  # the clickable "save" row does what enter does
            await settle(pilot, lambda: not isinstance(app.screen, KeyScreen) and engine.route.key == "k-kimi-second")
            assert f"ROCKYCODE_KIMI_API_KEY={SECRET}" not in ENV.read_text() and "k-kimi-second" in ENV.read_text()
            assert engine.model == "kimi-k3", "the model in use reopens with the new key"

            rows = await open_key_list()  # another endpoint, then cancelled
            rows.highlighted = next(i for i, (_, ep) in enumerate(app.screen.rows) if ep.eid == "minimax")
            rows.action_select()
            await settle(pilot, lambda: isinstance(app.screen, KeyScreen))
            assert app.screen.key_env == "ROCKYCODE_MINIMAX_API_KEY"
            await pilot.press("escape")
            await settle(pilot, lambda: not isinstance(app.screen, KeyScreen))
            await pilot.pause(0.1)
            assert "no key saved" in transcript(app) and "ROCKYCODE_MINIMAX_API_KEY" not in ENV.read_text()
            assert engine.model == "kimi-k3"

            await open_key_list()  # esc on the list: nothing said, nothing changed
            before = transcript(app)
            await pilot.press("escape")
            await settle(pilot, lambda: not isinstance(app.screen, KeyPicker))
            assert transcript(app) == before and app.focused is app.query_one(ChatInput)
            print("change: /model → add or change a key → pick the endpoint → the same hidden field  ✓")

            # ── with an OS keychain: its row comes first, and ↵ saves there ───────
            chain = {}
            fake = types.SimpleNamespace(get_password=lambda service, name: chain.get((service, name)),
                                         set_password=lambda service, name, value: chain.update({(service, name): value}),
                                         delete_password=lambda service, name: chain.pop((service, name)))
            with patch.object(onboarding, "_keyring", lambda: fake):
                rows = await open_key_list()
                listed = [str(rows.get_option_at_index(i).prompt) for i in range(rows.option_count)]
                assert "move the keys in the key file to the OS keychain (2)" in listed[-1], listed[-1]
                rows.highlighted = next(i for i, (_, ep) in enumerate(app.screen.rows) if ep.eid == "minimax")
                rows.action_select()
                await settle(pilot, lambda: isinstance(app.screen, KeyScreen))
                actions = app.screen.query_one("#key-actions", OptionList)
                assert [actions.get_option_at_index(i).id for i in range(actions.option_count)] == ["keychain", "file", "cancel"]
                assert "↵ saves in the OS keychain" in str(app.screen.query_one("#key-guide", Static).render())
                app.screen.query_one("#key-input", Input).value = "k-minimax-chain"
                await pilot.press("enter")
                await settle(pilot, lambda: not isinstance(app.screen, KeyScreen))
                await pilot.pause(0.1)
                assert chain == {("rockycode", "ROCKYCODE_MINIMAX_API_KEY"): "k-minimax-chain"}, list(chain)
                assert "ROCKYCODE_KEYCHAIN=ROCKYCODE_MINIMAX_API_KEY" in ENV.read_text().splitlines()
                assert "k-minimax-chain" not in ENV.read_text(), "the key file holds the name, never the key"
                assert "key saved to the OS keychain as ROCKYCODE_MINIMAX_API_KEY" in transcript(app)
                assert os.environ["ROCKYCODE_MINIMAX_API_KEY"] == "k-minimax-chain"

                rows = await open_key_list()  # the key file row instead: one key, one home
                rows.highlighted = next(i for i, (_, ep) in enumerate(app.screen.rows) if ep.eid == "minimax")
                assert "key in the OS keychain" in str(rows.get_option_at_index(rows.highlighted).prompt)
                rows.action_select()
                await settle(pilot, lambda: isinstance(app.screen, KeyScreen))
                app.screen.query_one("#key-input", Input).value = "k-minimax-file"
                actions = app.screen.query_one("#key-actions", OptionList)
                actions.highlighted = 1
                actions.action_select()
                await settle(pilot, lambda: not isinstance(app.screen, KeyScreen))
                await pilot.pause(0.1)
                assert not chain and "ROCKYCODE_MINIMAX_API_KEY=k-minimax-file" in ENV.read_text().splitlines()
                assert "ROCKYCODE_KEYCHAIN" not in ENV.read_text()

                rows = await open_key_list()  # the last row: every key in the file, without pasting one again
                assert "(3)" in str(rows.get_option_at_index(rows.option_count - 1).prompt)
                rows.highlighted = rows.option_count - 1
                rows.action_select()
                await settle(pilot, lambda: "moved to the OS keychain" in transcript(app))
                assert chain == {("rockycode", "ROCKYCODE_API_KEY"): "k-home-fixture",
                                 ("rockycode", "ROCKYCODE_KIMI_API_KEY"): "k-kimi-second",
                                 ("rockycode", "ROCKYCODE_MINIMAX_API_KEY"): "k-minimax-file"}, list(chain)
                assert ENV.read_text().splitlines() == [
                    "# my notes", "ROCKYCODE_KEYCHAIN=ROCKYCODE_API_KEY,ROCKYCODE_KIMI_API_KEY,ROCKYCODE_MINIMAX_API_KEY"]
                assert engine.route.key == "k-kimi-second" and engine.model == "kimi-k3", "the session keeps running"
                rows = await open_key_list()
                listed = [str(rows.get_option_at_index(i).prompt) for i in range(rows.option_count)]
                assert not any("move the keys" in row for row in listed), "nothing left in the file"
                assert sum("key in the OS keychain" in row for row in listed) >= 3, listed
                await pilot.press("escape")
                await settle(pilot, lambda: not isinstance(app.screen, KeyPicker))

                # A keychain that does not take the key: nothing is saved, the field stays open
                # with the key in it, and the key file is a row the user picks.
                fake.set_password = lambda *a: (_ for _ in ()).throw(RuntimeError("the keychain is locked"))
                rows = await open_key_list()
                rows.highlighted = next(i for i, (_, ep) in enumerate(app.screen.rows) if ep.eid == "glm")
                rows.action_select()
                await settle(pilot, lambda: isinstance(app.screen, KeyScreen))
                screen, kept_file = app.screen, ENV.read_text()
                screen.query_one("#key-input", Input).value = "k-glm-refused"
                await pilot.press("enter")
                await settle(pilot, lambda: "nothing is saved yet" in str(screen.query_one("#key-status", Static).render()))
                assert app.screen is screen, "the field stays open"
                assert screen.query_one("#key-input", Input).value == "k-glm-refused", "with the key still in it"
                assert ENV.read_text() == kept_file and "ROCKYCODE_GLM_API_KEY" not in os.environ
                assert ("rockycode", "ROCKYCODE_GLM_API_KEY") not in chain
                actions = screen.query_one("#key-actions", OptionList)
                actions.highlighted = 1  # the key file row
                actions.action_select()
                await settle(pilot, lambda: not isinstance(app.screen, KeyScreen))
                await pilot.pause(0.1)
                assert "ROCKYCODE_GLM_API_KEY=k-glm-refused" in ENV.read_text().splitlines()
                assert os.environ["ROCKYCODE_GLM_API_KEY"] == "k-glm-refused"
            print("keychain: its row comes first and ↵ saves there; the list says where; one row moves the rest  ✓")
            print("keychain: one that does not take the key saves nothing; the key file is picked, never fallen into  ✓")

            # ── the key never left the field ──────────────────────────────────────
            for secret in (SECRET, "k-kimi-second", "k-typo", "k-minimax-chain", "k-minimax-file", "k-glm-refused"):
                assert secret not in transcript(app), "not in the transcript"
                assert secret not in "\n".join(app.query_one(ChatInput)._history), "not in the input history"
                assert secret not in engine.trajectory.path.read_text(), "not in the session record"
                assert not any(secret in str(m.get("content")) for m in engine.history), "not in the conversation"
            print("secret: the key is in the key file and the process, nowhere else  ✓")

            # ── no command takes a key: one typed by habit is not shown and not kept ──
            assert "/key" not in [c.name for c in commands.COMMANDS] and "/key" not in commands.help_text(app)
            box = app.query_one(ChatInput)
            shaped = "sk-" + "Ab12" * 8       # looks like a key (built here: never a literal)
            for pasted in (shaped, "k-kimi-second"):  # the second is the key now in use, no known shape
                box.focus()
                box.text = f"/key {pasted}"
                await pilot.press("enter")
                await pilot.pause(0.2)
                assert not isinstance(app.screen, (KeyScreen, KeyPicker))
                assert pasted.lower() not in transcript(app).lower(), "not echoed"
                assert pasted not in "\n".join(box._history), "not in the up-arrow list"
            kept = home / "prompt-history.jsonl"
            assert shaped not in kept.read_text() and "k-kimi-second" not in kept.read_text(), "not on disk"
            assert "/key [redacted" in kept.read_text(), kept.read_text()
            assert stat.S_IMODE(kept.stat().st_mode) == 0o600, "and what is kept is the user's alone"
            assert "i no know" in transcript(app) and "/key" in transcript(app), "an unknown command, named without its argument"
            print("no /key: a key typed after a command is masked in the history and never echoed  ✓")


asyncio.run(main())
print("TUI KEY SMOKE OK — one key, pasted into the one field made for it. amaze!")
