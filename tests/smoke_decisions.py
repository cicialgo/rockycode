"""Offline backend, policy, YOLO observation, and paired-evaluation regressions."""
from __future__ import annotations

import asyncio
import copy
import json
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from rockycode.decision_eval import evaluate, load_cases, summarize
from rockycode.engine.verifier import (
    PERPLEXITY_URL, PPLX_MODEL, SYSTEMONE_URL, ChoiceQuestion, DecisionClient, DecisionConfig,
)
from rockycode.engine.loop import Engine
from rockycode.engine.verifier import VerifierConfig, VerifierGate, load_config
from rockycode.tui.app import RockyCodeApp

QUESTION = ChoiceQuestion("Pick an outcome", {"allow": "proceed", "ask": "ask human", "deny": "refuse"}, "gate")


def local_body(masses=(.96, .03, .01)):
    chosen = chr(65 + max(range(len(masses)), key=lambda i: masses[i]))
    return {"model": "qwen3.5:4b", "done": True, "message": {"content": chosen},
            "logprobs": [{"token": chosen, "logprob": math.log(max(masses)),
                          "top_logprobs": [{"token": chr(65 + i), "logprob": math.log(p)}
                                           for i, p in enumerate(masses)]}]}


def remote_body(name="gate", options=None):
    options = options or {"allow": .96, "ask": .03, "deny": .01}
    return {"model": "jev-1.13.0", "answers": {name: {"type": "choice",
            "choice": max(options, key=options.get), "probabilities": options, "confidence": .91}}}


class Script:
    def __init__(self, body=None, status=200, error=None):
        self.body = local_body() if body is None else body
        self.status, self.error = status, error
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.status, json.dumps(self.body)


class Decisions(unittest.IsolatedAsyncioTestCase):
    async def test_local_scores_are_conditional_not_provider_confidence(self):
        script = Script(local_body((.48, .015, .005)))
        client = DecisionClient(DecisionConfig("ollama", "qwen3.5:4b"), transport=script,
                                environ={"TYPESAFE_API_KEY": "must-not-send",
                                         "ROCKYCODE_VERIFIER_API_KEY": "also-must-not-send"})
        result = await client.choose({"example": "state"}, QUESTION)
        self.assertIsNone(result.error)
        self.assertIsNone(result.confidence)
        self.assertAlmostEqual(result.score, .96)
        self.assertAlmostEqual(result.candidate_mass, .5)
        self.assertEqual(result.score_kind, "conditional_option_probability")
        call = script.calls[0]
        self.assertEqual(call["url"], "http://127.0.0.1:11434/api/chat")
        self.assertNotIn("Authorization", call["headers"])
        self.assertFalse(call["body"]["think"])
        self.assertEqual(call["body"]["options"]["num_predict"], 1)
        self.assertNotIn("format", call["body"])

    async def test_non_approval_choices_work_on_both_backends(self):
        question = ChoiceQuestion("Select a model", {"small": "easy", "large": "hard"}, "routing")
        for provider, body in [("typesafe", remote_body("routing", {"small": .96, "large": .04})),
                               ("ollama", local_body((.96, .04)))]:
            result = await DecisionClient(DecisionConfig(provider), transport=Script(body),
                                          environ={"TYPESAFE_API_KEY": "fake-key"}).choose("easy task", question)
            self.assertEqual((result.choice, result.error), ("small", None))
            self.assertEqual(set(result.probabilities), {"small", "large"})

    async def test_missing_invalid_or_ambiguous_local_scores_fail_closed(self):
        bodies = []
        b = local_body(); b.pop("logprobs"); bodies.append(b)
        b = local_body(); b["logprobs"][0]["top_logprobs"].pop(); bodies.append(b)
        b = local_body(); b["logprobs"][0]["token"] = "\n"; bodies.append(b)
        b = local_body(); b["message"]["content"] = "A with confidence 1.0"; bodies.append(b)
        b = local_body(); b["message"]["thinking"] = "thinking"; bodies.append(b)
        b = local_body(); b["done"] = False; bodies.append(b)
        b = local_body(); b["logprobs"].append(copy.deepcopy(b["logprobs"][0])); bodies.append(b)
        b = local_body(); b["logprobs"][0]["top_logprobs"].append({"token": "A", "logprob": -.1}); bodies.append(b)
        for value in (float("nan"), float("inf"), True, .1, "-.1"):
            b = local_body(); b["logprobs"][0]["top_logprobs"][0]["logprob"] = value; bodies.append(b)
        bodies.append(local_body((.9, .9, .9)))
        for body in bodies:
            with self.subTest(body=body):
                result = await DecisionClient(DecisionConfig("ollama"), transport=Script(body)).choose({}, QUESTION)
                self.assertEqual(result.error, "malformed")
                self.assertIsNone(result.choice)

    async def test_transport_deadline_cancellation_redirect_and_errors(self):
        async def hanging(**kwargs):
            await asyncio.Event().wait()
        result = await DecisionClient(DecisionConfig("ollama", timeout_ms=10), transport=hanging).choose({}, QUESTION)
        self.assertEqual(result.error, "timeout")
        with self.assertRaises(asyncio.CancelledError):
            await DecisionClient(DecisionConfig("ollama"), transport=Script(error=asyncio.CancelledError())).choose(
                {}, QUESTION)
        for status in (302, 429, 500):
            script = Script(status=status)
            result = await DecisionClient(DecisionConfig("ollama"), transport=script).choose({}, QUESTION)
            self.assertEqual(result.error, f"http_{status}")
            self.assertEqual(len(script.calls), 1)
        import httpx
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(302, headers={"location": "https://example.invalid"})
        original = httpx.AsyncClient
        with patch("httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(handler), **kw)):
            result = await DecisionClient(DecisionConfig("ollama")).choose({}, QUESTION)
        self.assertEqual((result.error, len(requests)), ("http_302", 1))

    async def test_endpoint_validation_and_missing_key_do_not_send(self):
        for url in ("http://example.invalid/api/chat", "https://[broken", "https://user:pass@example.invalid",
                    "http://127.0.0.1:99999/api/chat", "https://example.invalid/#fragment"):
            script = Script()
            result = await DecisionClient(DecisionConfig("ollama", base_url=url), transport=script).choose({}, QUESTION)
            self.assertEqual((result.error, script.calls), ("bad_base_url", []))
        script = Script()
        result = await DecisionClient(DecisionConfig(), transport=script, environ={}).choose({}, QUESTION)
        self.assertEqual((result.error, script.calls), ("missing_api_key", []))

    async def test_perplexity_and_local_systemone_share_the_choice_body(self):
        body = remote_body()
        script = Script(body)
        result = await DecisionClient(DecisionConfig("perplexity", "jev-1.13.0"), transport=script,
                                      environ={"PERPLEXITY_API_KEY": "pplx-key",
                                               "TYPESAFE_API_KEY": "must-not-send"}).choose({"t": 1}, QUESTION)
        self.assertEqual((result.choice, result.error, result.score_kind), ("allow", None, "provider_confidence"))
        call = script.calls[0]
        self.assertEqual(call["url"], PERPLEXITY_URL)
        self.assertEqual(call["body"]["model"], PPLX_MODEL)
        self.assertEqual(call["headers"]["Authorization"], "Bearer pplx-key")
        self.assertEqual(set(call["body"]["questions"]["gate"]["criteria"]), {"allow", "ask", "deny"})

        script = Script(body)
        result = await DecisionClient(
            DecisionConfig("systemone", base_url="http://127.0.0.1:8092/v1/systemone"),
            transport=script, environ={"PERPLEXITY_API_KEY": "must-not-send", "TYPESAFE_API_KEY": "also-not"},
        ).choose({"t": 1}, QUESTION)
        self.assertIsNone(result.error)
        self.assertEqual(script.calls[0]["url"], SYSTEMONE_URL)
        self.assertNotIn("Authorization", script.calls[0]["headers"])
        self.assertEqual(script.calls[0]["body"]["questions"], call["body"]["questions"])


class Gate(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def gate(self, *, config=None, script=None):
        return VerifierGate(config or VerifierConfig(mode="enforce", provider="ollama", model="qwen3.5:4b"),
                            transport=script or Script(), environ={})

    async def review(self, gate, mode="ask", **kw):
        args = dict(mode=mode, risk="risky", tool="bash", args={"command": "ls"}, workdir=self.root)
        args.update(kw)
        return await gate.review(**args)

    async def test_local_enforce_uses_separate_threshold(self):
        gate = self.gate(script=Script(local_body((.90, .09, .01))))
        got = await self.review(gate)
        self.assertEqual(got.effective, "ask")  # > .85 Jev floor but < .95 local floor
        self.assertIsNone(got.record["confidence"])
        self.assertEqual(got.record["threshold"], .95)
        self.assertEqual((await self.review(self.gate())).effective, "allow")

    async def test_shadow_all_yolo_observes_but_cannot_deny_or_grant(self):
        cfg = VerifierConfig(mode="shadow", scope="all", provider="ollama", max_calls=1, fail="deny")
        script = Script(local_body((.01, .03, .96)))
        gate = self.gate(config=cfg, script=script)
        first = await self.review(gate, mode="yolo")
        self.assertEqual((first.static, first.effective, first.applied, first.record["choice"]),
                         ("allow", "allow", False, "deny"))
        second = await self.review(gate, mode="yolo")
        self.assertEqual((second.effective, second.record["error"], len(script.calls)), ("allow", "budget", 1))
        for error in (TimeoutError(), RuntimeError("secret error text")):
            got = await self.review(self.gate(config=cfg, script=Script(error=error)), mode="yolo")
            self.assertEqual((got.effective, got.applied), ("allow", False))
            self.assertNotIn("secret error text", json.dumps(got.record))
        script = Script()
        gate = self.gate(config=cfg, script=script)
        got = await self.review(gate, mode="yolo", args={"command": "sudo rm -rf /"})
        self.assertEqual((got.effective, script.calls), ("block", []))
        got = await self.review(gate, session_allows=True)
        self.assertEqual((got.effective, script.calls), ("allow", []))
        # Enforce cannot grow a new YOLO interception policy through scope=all.
        got = await self.review(self.gate(config=replace(cfg, mode="enforce"), script=script), mode="yolo")
        self.assertEqual((got.effective, script.calls), ("allow", []))

    async def test_tui_reaches_shadow_on_yolo_and_preserves_approval(self):
        gate = self.gate(config=VerifierConfig(mode="shadow", scope="all", provider="ollama"))
        engine = Engine(model="fake", client=SimpleNamespace(), workdir=self.root)
        notes = []
        engine.trajectory = SimpleNamespace(note=notes.append)
        app = RockyCodeApp(engine, permission="yolo")
        app._jev = gate
        app._add = AsyncMock()
        app._ask_inline = AsyncMock(return_value="deny")
        self.assertTrue(await app._approve_tool("bash", {"command": "ls"}))
        self.assertEqual(notes[0]["verifier"]["static"], "allow")
        self.assertEqual(gate.calls, 1)
        app._ask_inline.assert_not_awaited()

    async def test_configuration_precedence_and_project_isolation(self):
        global_path = self.root / "global.toml"
        global_path.write_text('[jev]\nauto="enforce"\n[verifier]\nauto="shadow"\nprovider="ollama"\nscope="all"\n')
        cfg = load_config({}, global_path=global_path)
        self.assertEqual((cfg.mode, cfg.provider, cfg.model, cfg.timeout_ms, cfg.scope),
                         ("shadow", "ollama", "qwen3.5:4b", 5000, "all"))
        cfg = load_config({"ROCKYCODE_JEV_AUTO": "enforce", "ROCKYCODE_VERIFIER_AUTO": "off"},
                          global_path=global_path)
        self.assertEqual(cfg.mode, "off")
        global_path.write_text('[jev]\nauto="enforce"\n[verifier]\nmode="off"\n')
        self.assertEqual(load_config({}, global_path=global_path).mode, "off")
        (self.root / ".rockycode").mkdir()
        (self.root / ".rockycode/config.toml").write_text('[verifier]\nauto="enforce"\nprovider="ollama"\n')
        with patch("os.getcwd", return_value=str(self.root)):
            self.assertEqual(load_config({}, global_path=self.root / "absent").mode, "off")

    async def test_paired_evaluation_uses_same_state_and_never_runs_commands(self):
        cases = load_cases(Path(__file__).resolve().parents[1] / "examples/verifier-cases.jsonl")[:3]
        calls = []
        async def transport(**kwargs):
            calls.append(kwargs)
            return 200, json.dumps(local_body() if "/api/chat" in kwargs["url"] else remote_body())
        with patch("asyncio.create_subprocess_shell", side_effect=AssertionError("must not execute")):
            rows = await evaluate(cases, [DecisionConfig(), DecisionConfig("ollama", "qwen3.5:4b")],
                                  transport=transport, environ={"TYPESAFE_API_KEY": "fake-key"})
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]["case_digest"], rows[1]["case_digest"])
        self.assertEqual(calls[0]["body"]["state"], json.loads(calls[1]["body"]["messages"][1]["content"]))
        self.assertNotIn("args_summary", json.dumps(rows))
        summary = summarize(rows)
        self.assertEqual(summary["ollama"]["wrong_allow"], 1)
        self.assertEqual(summary["typesafe"]["valid"], 3)


if __name__ == "__main__":
    unittest.main()
