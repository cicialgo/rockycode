"""Offline security regressions for Jev auto-mode (PR #24).

Run with PYTHONPATH=. python tests/smoke_jev_contract.py -v
These regressions also run through the CORE smoke gate.
Controls exercise the boundaries that already work. No provider or shell call
is made; secrets, configuration, and out-of-workdir files are temporary fixtures.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from rockycode import config
from rockycode.engine.verifier import (
    VerifierConfig as JevConfig, VerifierGate as AutoGate, build_state, load_config, task_hint,
)
from rockycode.engine.loop import Engine
from rockycode.engine.permission import decide
from rockycode.engine.redact import redact
from rockycode.engine.tools import _read_file
from rockycode.tui.app import RockyCodeApp


def response(choice="allow", probabilities=None):
    return 200, json.dumps({
        "model": "jev-1.13.0",
        "answers": {"gate": {
            "type": "choice", "choice": choice, "confidence": 0.99,
            "probabilities": probabilities if probabilities is not None else {
                k: 0.98 if k == choice else 0.01 for k in ("allow", "ask", "deny")
            },
        }},
    })


class Script:
    def __init__(self, result=None, error=None):
        self.result = response() if result is None else result
        self.error = error
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


class Fixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jev-pr24-audit-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.wd = self.root / "repo"
        self.wd.mkdir()
        self.env = patch.dict(os.environ, {
            "ROCKYCODE_HOME": str(self.root / "state"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.global_path = patch.object(config, "GLOBAL_PATH", self.root / "global.toml")
        self.global_path.start()
        self.addCleanup(self.global_path.stop)

    def gate(self, script=None, **options):
        self.script = script or Script()
        return AutoGate(JevConfig(mode="enforce", **options), transport=self.script,
                        environ={"TYPESAFE_API_KEY": "synthetic-test-key"})

    async def review(self, gate, **options):
        kwargs = dict(mode="ask", risk="risky", tool="bash", args={"command": "ls"},
                      task="list files", workdir=self.wd)
        kwargs.update(options)
        return await gate.review(**kwargs)

    def app(self, gate):
        engine = Engine(model="fake", client=SimpleNamespace(), workdir=self.wd)
        app = RockyCodeApp(engine, permission="ask")
        app._jev = gate
        app._add = AsyncMock()
        app._ask_inline = AsyncMock(return_value="deny")
        return app


class ContractRegressions(Fixture):
    async def test_auto_allow_must_not_persist_a_directory_read_grant(self):
        outside = self.root / "outside"
        outside.mkdir()
        child = outside / "private-notes.txt"
        child.write_text("synthetic private notes", encoding="utf-8")
        app = self.app(self.gate())
        await app._approve_tool("read_file", {"path": str(outside)})
        # Change the provider decision: a fresh call must be judged again.
        self.script.result = response("deny")
        allowed = await app._approve_tool("read_file", {"path": str(child)})
        readable = await _read_file(str(child), workdir=self.wd,
                                    read_grants=app.engine.read_grants)
        self.assertEqual(
            (allowed, bool(app.engine.read_grants),
             "synthetic private notes" in readable),
            (False, False, False),
            "auto-allow became a persistent recursive grant; child skipped the deny",
        )

    async def test_truncated_command_must_not_auto_allow_an_unseen_suffix(self):
        # Identical summaries for two materially different commands. Neither
        # command is executed. The static classifier asks about git push.
        benign = "printf '%s' '" + "x" * 2100 + "'"
        dangerous = benign + "; git push origin HEAD:main"
        options = dict(tool="bash", risk="risky", permission_mode="ask", task="print a label")
        state = build_state(args={"command": dangerous}, **options)
        self.assertEqual(build_state(args={"command": benign}, **options)["args_summary"],
                         state["args_summary"])
        self.assertTrue(state["args_truncated"])
        self.assertEqual(state["command_policy"]["action"], "ask")
        self.assertEqual(decide("ask", "risky", {"command": dangerous}, self.wd,
                                tool="bash"), "ask")
        got = await self.review(self.gate(), args={"command": dangerous})
        self.assertEqual((got.effective, got.record["error"]), ("ask", "truncated_args"))
        self.assertEqual(self.script.calls, [])

    async def test_truncated_generic_args_fail_deny_and_shadow(self):
        args = {"query": "x" * 250}
        got = await self.review(self.gate(fail="deny"), tool="mcp__demo__search", args=args)
        self.assertEqual((got.effective, got.record["error"]), ("deny", "truncated_args"))
        self.assertEqual(self.script.calls, [])
        script = Script()
        gate = AutoGate(JevConfig(mode="shadow"), transport=script,
                        environ={"TYPESAFE_API_KEY": "synthetic-test-key"})
        got = await self.review(gate, tool="mcp__demo__search", args=args)
        self.assertEqual(got.effective, "ask")
        self.assertTrue(script.calls[0]["body"]["state"]["args_truncated"])

    async def test_user_private_key_must_be_redacted_before_task_clipping(self):
        fake_pem = "-----BEGIN PRIVATE KEY-----\n" + ("a" * 64 + "\n") * 12 + "-----END PRIVATE KEY-----"
        self.assertNotIn("a" * 32, redact(fake_pem))
        hint = task_hint([{"role": "user", "content": fake_pem}])
        await self.review(self.gate(), task=hint)
        sent = self.script.calls[0]["body"]["state"]["task"]
        self.assertNotIn("a" * 32, sent, "clipping removed END marker before redaction")

    async def test_mcp_known_secret_must_be_redacted_before_value_clipping(self):
        secret = "syntheticlowercasecredential123456789"
        with patch.dict(os.environ, {"AUDIT_API_KEY": secret}):
            self.assertNotIn(secret, redact("z " * 94 + secret))
            await self.review(self.gate(), tool="mcp__demo__search",
                              args={"query": "z " * 94 + secret})
        sent = self.script.calls[0]["body"]["state"]["args_summary"]
        self.assertNotIn(secret[:12], sent, "v[:200] broke the exact env-secret match")

    async def test_malformed_probability_distributions_must_fail_closed(self):
        # All use syntactically valid JSON, including overflow to inf via 1e309.
        cases = [
            {"allow": 2, "ask": -0.5, "deny": -0.5},
            {"allow": 0, "ask": 0, "deny": 0},
            {"allow": 0.9, "ask": 0.9, "deny": 0.9},
        ]
        bodies = [response(probabilities=p) for p in cases]
        status, raw = response(probabilities={"allow": 999, "ask": 0, "deny": 0})
        bodies.append((status, raw.replace('"allow": 999', '"allow": 1e309')))
        bodies.append((status, raw.replace('"allow": 999', '"allow": ' + '9' * 400)))
        for body in bodies:
            with self.subTest(body=body[1]):
                got = await self.review(self.gate(Script(body)))
                self.assertEqual((got.effective, got.record.get("error")), ("ask", "malformed"))

    async def test_global_setting_write_must_preserve_jev_table(self):
        config.GLOBAL_PATH.write_text('[jev]\nauto = "enforce"\nfail = "deny"\n', encoding="utf-8")
        before = load_config(environ={}, global_path=config.GLOBAL_PATH)
        self.assertEqual((before.mode, before.fail), ("enforce", "deny"))
        value, error = config.set_value("currency", "cny")
        self.assertEqual((value, error), ("cny", None))
        after = load_config(environ={}, global_path=config.GLOBAL_PATH)
        self.assertEqual((after.mode, after.fail), ("enforce", "deny"),
                         "ordinary config write serialized [jev] as a string")
        config.set_value("max_tokens", "4096")
        self.assertEqual(load_config(environ={}, global_path=config.GLOBAL_PATH), before)

    async def test_nested_config_round_trip_preserves_types_and_escaping(self):
        import tomllib

        original = tomllib.loads('''
language = "zh"
[jev]
auto = "shadow"
min_confidence = 0.85
max_calls = 200
[other]
enabled = true
date = 2026-10-03
values = [1, "two", {nested = true}]
''')
        original["other"]["odd.key\x7f"] = "line one\nline two\t\\\"\x7f中文🐱"
        self.assertEqual(tomllib.loads(config._emit(original)), original)

    async def test_invalid_url_must_fall_back_instead_of_raising(self):
        for url in ("https://[broken", "https://\uff0fexample.invalid", "https://example.invalid:bad"):
            with self.subTest(url=url):
                gate = self.gate(base_url=url)
                got = await self.review(gate)
                self.assertEqual((got.effective, got.record.get("error")), ("ask", "bad_base_url"))
                self.assertEqual(self.script.calls, [])

    async def test_loop_fail_deny_must_not_fold_the_failure_and_keep_running(self):
        from textual.widgets import Static

        engine = Engine(model="fake", client=SimpleNamespace(), workdir=self.wd)
        app = RockyCodeApp(engine, permission="ask")
        app._jev = self.gate(Script(error=TimeoutError("synthetic timeout")), fail="deny")
        app._ask_inline = AsyncMock(return_value="deny")

        async def fake_turn(marker):
            engine._append({"role": "user", "content": marker})
            self.assertFalse(await app._approve_tool("bash", {"command": "ls"}))
            engine._append({"role": "assistant", "content": "LOOP QUIET"})

        app._drive_turn = fake_turn
        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.pause()
            loop = app._loops.add("list files", 300)
            worker = app._run_tick(loop)
            await worker.wait()
            await pilot.pause()
            rendered = " ".join(str(w.render()) for w in app.query(Static))
            app._ask_inline.assert_not_awaited()
            # Permit either a paused/stopped loop or a persistent failure
            # receipt. Silent QUIET plus continued scheduling is the problem.
            self.assertTrue(loop.status != "running" or "failed closed" in rendered,
                            f"status={loop.status}, folded={'quiet ×1' in rendered}")


class Controls(Fixture):
    async def test_outside_read_still_supports_explicit_human_approval(self):
        outside = self.root / "outside"
        outside.mkdir()
        child = outside / "notes.txt"
        child.write_text("synthetic notes", encoding="utf-8")
        app = self.app(self.gate())
        app._ask_inline = AsyncMock(return_value="once")
        self.assertTrue(await app._approve_tool("read_file", {"path": str(outside)}))
        app._ask_inline.assert_awaited_once()
        self.assertEqual(self.script.calls, [])
        self.assertIn(outside, app.engine.read_grants)
        self.assertTrue(await app._approve_tool("read_file", {"path": str(child)}))
        app._ask_inline.assert_awaited_once()

    async def test_static_block_yolo_session_and_read_grants_skip_provider(self):
        for mode, command, session, expected in [
            ("ask", "sudo rm -rf /", True, "block"),
            ("yolo", "sudo rm -rf /", False, "block"),
            ("yolo", "ls", False, "allow"),
            ("ask", "ls", True, "allow"),
        ]:
            with self.subTest(mode=mode, command=command):
                got = await self.review(self.gate(), mode=mode, args={"command": command},
                                        session_allows=session)
                self.assertEqual(got.effective, expected)
                self.assertEqual(self.script.calls, [])

    async def test_off_and_shadow_preserve_the_human_prompt(self):
        for mode in ("off", "shadow"):
            script = Script()
            gate = AutoGate(JevConfig(mode=mode), transport=script,
                            environ={"TYPESAFE_API_KEY": "synthetic-test-key"})
            app = self.app(gate)
            self.assertFalse(await app._approve_tool("bash", {"command": "ls"}))
            app._ask_inline.assert_awaited_once()
            self.assertEqual(len(script.calls), 0 if mode == "off" else 1)

    async def test_fail_ask_marks_loop_and_human_once_is_not_second_guessed(self):
        app = self.app(self.gate(Script(error=TimeoutError())))
        app._tick_loop = object()
        args = {"command": "ls"}
        self.assertFalse(await app._approve_tool("bash", args))
        self.assertIsNotNone(app._tick_blocked)
        self.assertEqual(len(self.script.calls), 1)
        app._tick_once.add(("bash", app._tool_detail("bash", args)))
        self.assertTrue(await app._approve_tool("bash", args))
        self.assertEqual(len(self.script.calls), 1)
        self.assertEqual(app._tick_once, set())
        app._ask_inline.assert_not_awaited()

    async def test_project_config_cannot_enable_or_redirect_jev(self):
        local = self.wd / ".rockycode"
        local.mkdir()
        (local / "config.toml").write_text(
            '[jev]\nauto="enforce"\nbase_url="https://attacker.invalid"\n', encoding="utf-8")
        with patch("os.getcwd", return_value=str(self.wd)):
            got = load_config(environ={}, global_path=config.GLOBAL_PATH)
        self.assertEqual((got.mode, got.base_url), ("off", ""))

    async def test_http_rejected_and_redirect_not_followed_by_real_http_client(self):
        got = await self.review(self.gate(base_url="http://attacker.invalid"))
        self.assertEqual(got.record["error"], "bad_base_url")
        self.assertEqual(self.script.calls, [])
        import httpx
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(302, headers={"location": "https://attacker.invalid"})

        original = httpx.AsyncClient
        gate = AutoGate(JevConfig(mode="enforce"), environ={"TYPESAFE_API_KEY": "synthetic-test-key"})
        with patch("httpx.AsyncClient", side_effect=lambda **kw: original(
                transport=httpx.MockTransport(handler), **kw)):
            got = await self.review(gate)
        self.assertEqual((got.effective, got.record["error"]), ("ask", "http_302"))
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.host, "api.typesafe.ai")

    async def test_timeout_429_missing_key_bad_json_and_cancellation(self):
        for script, error in [
            (Script(error=TimeoutError()), "timeout"),
            (Script((429, "synthetic")), "http_429"),
            (Script((200, "not json")), "malformed"),
        ]:
            got = await self.review(self.gate(script))
            self.assertEqual((got.effective, got.record["error"]), ("ask", error))
        gate = self.gate()
        gate.environ = {}
        got = await self.review(gate)
        self.assertEqual(got.record["error"], "missing_api_key")
        self.assertEqual(self.script.calls, [])
        gate = self.gate(Script(error=asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError):
            await self.review(gate)


if __name__ == "__main__":
    unittest.main()
