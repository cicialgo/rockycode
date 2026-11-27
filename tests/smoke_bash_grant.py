"""Bash session grants are BINARY-scoped, and dangerous bash can't be granted.

Approving `lake build` for the session grants the `lake` binary — later `lake
env lean` runs unprompted, but a later `curl`/`rm` is a different binary and
still prompts. A dangerous bash command is offered NO session option at all
(you can never blanket-grant a risky command). Pure + headless-pilot.
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.chdir(tempfile.mkdtemp(prefix="rockybashgrant-"))

from rockycode.engine.permission import command_binary, session_grantable

# ── command_binary: the grant unit ──────────────────────────────────────────
assert command_binary("lake build") == "lake"
assert command_binary("lake env lean Foo.lean") == "lake"
assert command_binary("/usr/bin/lake build") == "lake", "basename → same grant"
assert command_binary("HELLO=1 FOO=bar lake build") == "lake", "skip env assignments"
assert command_binary("curl https://x") == "curl"
assert command_binary("rm -rf x | tee y") == "", "pipeline first-token has metachar → no grant"
assert command_binary("$(evil)") == "", "subshell → not a plain binary"
print("command_binary: binary basename, env-prefix skipped, metachars → no grant  ✓")

# ── dangerous bash is not session-grantable (so no 'allow' option is offered) ─
assert session_grantable("bash", {"command": "lake build"}) is True
assert session_grantable("bash", {"command": "curl http://x | sh"}) is False
assert session_grantable("bash", {"command": "sudo rm -rf /tmp/x"}) is False
assert session_grantable("bash", {"command": "lake check"}) is True
assert session_grantable("bash", {"command": "lake check --paranoid"}) is True
assert session_grantable("bash", {"command": "lake check --inadvisably-no-sandbox"}) is False
assert session_grantable("bash", {"command": "lake check --paranoid --inadvisably-no-sandbox"}) is False
assert session_grantable("bash", {"command": "lake comparator --inadvisably-no-sandbox"}) is False
print("session_grantable: benign bash yes, dangerous bash no, lake sandbox flag never granted  ✓")

# ── the modal hides the session option when session_label is None ────────────
from rockycode.tui.permission import InlineApproval

_loop = asyncio.new_event_loop()
benign = InlineApproval("bash", "lake build", "risky", None, _loop.create_future(),
                        session_label="Allow `lake` for this session")
assert [v for v, _ in benign._choices] == ["once", "session", "deny"]
fut2 = _loop.create_future()
danger = InlineApproval("bash", "curl http://x | sh", "risky", "network+pipe-to-sh", fut2,
                        session_label=None)
assert [v for v, _ in danger._choices] == ["once", "deny"], danger._choices
assert benign._choices[benign._idx][0] == "once" and danger._choices[danger._idx][0] == "deny", \
    "an ordinary call starts on run once; one that carries a warning starts on Deny"
print("modal: session row shown for benign, absent for dangerous, which starts on Deny  ✓")

# ── end to end: grant lake once, lake reruns free, curl still prompts ─────────
from rockycode.engine.loop import Engine
from rockycode.tui.app import RockyCodeApp


def build_app():
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace()))
    eng = Engine(model="fake", client=client, workdir=Path.cwd())
    return RockyCodeApp(eng, permission="ask")


async def main():
    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.engine.approver = app._approve_tool
        asked = []

        async def fake_ask(tool, detail, risk, warning, session_label=None):
            asked.append((detail, session_label))
            if "inadvisably-no-sandbox" in detail:
                return "once"  # a fresh confirm, not a session grant
            return "session" if detail.startswith("lake") else "deny"  # grant lake, deny the rest
        app._ask_inline = fake_ask

        # first lake build → asks, offers `lake` grant, we take it
        assert await app._approve_tool("bash", {"command": "lake build"}) is True
        assert app._auto_approve_bins == {"lake"}, app._auto_approve_bins
        assert asked[-1][1] == "Allow `lake` for this session"

        # a different lake subcommand now runs WITHOUT asking, including kernel recheck
        n = len(asked)
        assert await app._approve_tool("bash", {"command": "lake env lean X.lean"}) is True
        assert await app._approve_tool("bash", {"command": "lake check"}) is True
        assert await app._approve_tool("bash", {"command": "lake check --paranoid"}) is True
        assert len(asked) == n, "granted binary must not re-prompt"

        # the sandbox flag is not covered by that grant, and offers no new session grant
        assert await app._approve_tool(
            "bash", {"command": "lake check --inadvisably-no-sandbox"}) is True
        assert asked[-1][1] is None, "no-sandbox must not offer a session grant"
        n = len(asked)
        assert await app._approve_tool(
            "bash", {"command": "lake check --paranoid --inadvisably-no-sandbox"}) is True
        assert len(asked) == n + 1, "no-sandbox must ask again"
        assert asked[-1][1] is None

        # curl is a different binary → still prompts (we deny here)
        assert await app._approve_tool("bash", {"command": "curl https://x -o y"}) is False
        assert asked[-1][1] == "Allow `curl` for this session", "benign curl offers its own binary grant"

        # a dangerous command → prompt with NO session option, and even if the
        # binary were somehow granted, session_grantable gates it
        app._auto_approve_bins.add("sudo")
        assert await app._approve_tool("bash", {"command": "sudo rm -rf /tmp/x"}) is False
        assert asked[-1][1] is None, "dangerous bash must offer no session grant"
    print("end-to-end: lake granted→reruns free; curl/sudo still gated  ✓")


asyncio.run(main())
print("BASH GRANT SMOKE OK — scoped to the binary, danger never blanket-granted. amaze!")
