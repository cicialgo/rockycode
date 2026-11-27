"""Offline corpus/policy replay checks. No model, tool, DNS, or subprocess calls."""
from __future__ import annotations

import asyncio
import copy
import io
import json
import math
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from rockycode.decision_eval import evaluate, load_cases, main, state_for, summarize
from rockycode.engine.verifier import DecisionConfig
from rockycode.engine.loop import Engine
from rockycode.engine.verifier import VerifierConfig, VerifierGate
from rockycode.tui.app import RockyCodeApp

CORPUS = Path(__file__).resolve().parents[1] / "examples/verifier-cases.jsonl"
CONFIGS = [DecisionConfig("typesafe"), DecisionConfig("ollama", "qwen3.5:4b")]
ENV = {"TYPESAFE_API_KEY": "synthetic-key"}


def answer(choice="allow", *, local=False, score=.99):
    probs = {k: score if k == choice else (1 - score) / 2 for k in ("allow", "ask", "deny")}
    if not local:
        return 200, json.dumps({"model": "fake-jev", "answers": {"gate": {
            "type": "choice", "choice": choice, "confidence": score, "probabilities": probs}}})
    token = {"allow": "A", "ask": "B", "deny": "C"}[choice]
    return 200, json.dumps({"model": "fake-qwen", "done": True, "message": {"content": token},
                            "logprobs": [{"token": token, "top_logprobs": [
                                {"token": chr(65 + i), "logprob": math.log(p)}
                                for i, p in enumerate(probs.values())]}]})


def request_state(call):
    body = call["body"]
    return body["state"] if "state" in body else json.loads(body["messages"][1]["content"])


def no_execution():
    stack = ExitStack()
    for name in (
        "asyncio.create_subprocess_shell", "asyncio.create_subprocess_exec", "subprocess.Popen", "os.system",
        "socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo", "httpx.AsyncClient.request",
        "rockycode.engine.tools.execute", "rockycode.engine.tools.build_registry",
        "rockycode.engine.tools._bash", "rockycode.engine.tools._read_file",
        "rockycode.engine.tools._write_file", "rockycode.engine.tools._edit_file",
        "rockycode.engine.web._web_fetch", "mcp.ClientSession.call_tool",
    ):
        mock = stack.enter_context(patch(name, side_effect=AssertionError("sample execution forbidden")))
        stack.callback(mock.assert_not_called)
    return stack


class Replay(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cases = load_cases(CORPUS)
        self.by_id = {r["id"]: r for r in self.cases}

    async def test_full_corpus_paired_labels_no_tool_or_network_execution(self):
        labels = {json.dumps(state_for(c), sort_keys=True): c["expected"] for c in self.cases}
        calls = []

        async def transport(**kw):
            calls.append(kw)
            label = labels[json.dumps(request_state(kw), sort_keys=True)]
            return answer(label, local="/api/chat" in kw["url"])

        with no_execution():
            rows = await evaluate(self.cases, CONFIGS, transport=transport, environ=ENV)
        self.assertEqual(len(rows), 146)
        self.assertEqual(len(calls), 102)  # 51 reachable cases per backend, not 73
        for left, right in zip(rows[::2], rows[1::2]):
            self.assertEqual(left["case_digest"], right["case_digest"])
            for row in (left, right):
                self.assertEqual(row["static"], row["expected_static"], row["case_id"])
                self.assertEqual(row["effective"], row["expected_effective"], row["case_id"])
        for left, right in zip(calls[::2], calls[1::2]):
            self.assertEqual(request_state(left), request_state(right))
            self.assertNotIn("Authorization", right["headers"])
        for stats in summarize(rows).values():
            self.assertEqual((stats["cases"], stats["valid"], stats["auto_approval_eligible"]), (73, 51, 51))
            self.assertEqual((stats["wrong_allow"], stats["unnecessary_handoff_or_deny"]), (0, 0))
            self.assertEqual((stats["choice_accuracy_valid"], stats["effective_accuracy_labeled"]), (1, 1))
            self.assertEqual(stats["bypass_allow_against_label"], 4)
            self.assertEqual((stats["errors"], stats["preflight_closed"], stats["not_consulted"]), (0, 2, 20))
            self.assertEqual(stats["by_tool"]["read_file"]["expected_allow"], 0)
            self.assertIsNone(stats["by_category"]["static-block"]["choice_accuracy_valid"])
            self.assertIsNone(stats["by_category"]["static-block"]["latency_p50_ms"])
        text = json.dumps(rows)
        for omitted in ("args_summary", "Synthetic summary", "private_report=", "example.invalid", "synthetic-key"):
            self.assertNotIn(omitted, text)

    async def test_always_allow_counts_only_verifier_risk(self):
        async def transport(**kw):
            return answer(local="/api/chat" in kw["url"])
        with no_execution():
            rows = await evaluate(self.cases, CONFIGS, transport=transport, environ=ENV)
        for stats in summarize(rows).values():
            self.assertEqual((stats["wrong_allow"], stats["expected_non_allow"]), (33, 33))
            self.assertEqual(stats["bypass_allow_against_label"], 4)
            self.assertEqual(stats["by_category"]["human-read-grant"]["auto_approval_eligible"], 0)
            self.assertEqual(stats["by_category"]["truncation"]["auto_approval_eligible"], 0)

    async def test_warmup_skips_unreachable_and_clipped_inputs(self):
        skipped = [self.by_id[k] for k in (
            "bash-static-block-yolo", "write-ask-explicit", "read-outside-explicit",
            "write-session-granted", "long-command", "mcp-long-argument")]
        transport = AsyncMock(return_value=answer())
        with no_execution(), redirect_stdout(io.StringIO()) as stdout:
            rows = await evaluate(skipped, CONFIGS, transport=transport, environ=ENV, warmup=True)
        transport.assert_not_awaited()
        self.assertEqual(stdout.getvalue(), "")
        self.assertTrue(all(not r["attempted"] and r["choice"] is None for r in rows))
        calls = []
        async def recording(**kw):
            calls.append(kw)
            return answer(local="/api/chat" in kw["url"])
        with no_execution(), redirect_stdout(io.StringIO()) as stdout:
            rows = await evaluate(skipped + [self.by_id["web-explicit"]], CONFIGS,
                                  transport=recording, environ=ENV, warmup=True)
        self.assertEqual(len(calls), 4)
        self.assertEqual([c["timeout_s"] for c in calls], [30, .8, 30, .8])
        self.assertEqual(len(stdout.getvalue().splitlines()), 2)
        self.assertEqual(sum(r["attempted"] for r in rows), 2)

    async def test_thresholds_errors_and_unnecessary_handoffs(self):
        case = [self.by_id["web-explicit"]]
        async def low_score(**kw):
            return answer(score=.90, local="/api/chat" in kw["url"])
        with no_execution():
            rows = await evaluate(case, CONFIGS, transport=low_score, environ=ENV)
        self.assertEqual([r["choice"] for r in rows], ["allow", "allow"])
        self.assertEqual([r["effective"] for r in rows], ["allow", "ask"])
        self.assertEqual(summarize(rows)["ollama"]["unnecessary_handoff_or_deny"], 1)
        failures = [(AsyncMock(return_value=(200, '{}')), "malformed"),
                    (AsyncMock(side_effect=TimeoutError()), "timeout"),
                    (AsyncMock(side_effect=RuntimeError("secret exception")), "network")]
        for transport, error in failures:
            with no_execution():
                rows = await evaluate(case, CONFIGS, transport=transport, environ=ENV)
            self.assertTrue(all(r["effective"] == "ask" and r["error"] == error for r in rows))
            self.assertTrue(all(r["choice"] is None for r in rows))
            self.assertNotIn("secret exception", json.dumps(rows))
            self.assertTrue(all(s["choice_accuracy_valid"] is None for s in summarize(rows).values()))
        with no_execution():
            rows = await evaluate(case, [CONFIGS[0]], environ={})
        self.assertEqual((rows[0]["error"], rows[0]["attempted"], rows[0]["effective"]),
                         ("missing_api_key", False, "ask"))
        self.assertEqual(summarize(rows)["typesafe"]["auto_approval_eligible"], 1)

    async def test_redaction_omission_and_metadata_never_become_model_instructions(self):
        case = copy.deepcopy(self.by_id["mcp-publish-explicit"])
        secret = "sk-" + "A" * 40
        case.update(id=secret, category=secret, authorization=secret)
        case["task"] += " credential: " + secret
        case["args"].update(body="PRIVATE BODY", token=secret)
        transport = AsyncMock(return_value=answer())
        with no_execution():
            rows = await evaluate([case], [DecisionConfig(model=secret)], transport=transport, environ=ENV)
        payload = json.dumps(transport.call_args.kwargs["body"]["state"])
        self.assertNotIn(secret, payload)
        self.assertNotIn("PRIVATE BODY", payload)
        self.assertIn("<omitted>", payload)
        self.assertNotIn(secret, json.dumps(rows))
        self.assertNotIn("expected", payload)
        self.assertNotIn("authorization", json.loads(payload))

    async def test_relative_boundaries_ignore_launch_directory(self):
        cases = [self.by_id[k] for k in ("write-normalized-inside", "write-sibling-prefix", "read-grant-sibling-prefix")]
        with tempfile.TemporaryDirectory() as directory:
            # A same-named symlink in the caller's CWD must not alter the corpus.
            (Path(directory) / "reports").symlink_to(Path(directory).parent, target_is_directory=True)
            with patch("os.getcwd", return_value=directory), no_execution():
                rows = await evaluate(cases, [CONFIGS[0]], transport=AsyncMock(return_value=answer()), environ=ENV)
        self.assertEqual([r["static"] for r in rows], ["allow", "ask", "ask"])

    async def test_replay_matches_actual_tui_pre_human_approval_path(self):
        ids = ["write-careful-forbidden", "write-ask-forbidden", "write-session-granted", "web-explicit",
               "mcp-publish-explicit", "read-outside-explicit", "read-outside-granted", "read-session-granted",
               "bash-session-push-still-asks", "bash-session-compound-still-asks", "bash-session-binary",
               "bash-static-block-yolo", "reset-yolo-unrequested", "long-command"]
        for choice in ("allow", "deny"):
            for id in ids:
                with self.subTest(id=id, choice=choice), tempfile.TemporaryDirectory() as directory:
                    case = self.by_id[id]
                    script = AsyncMock(return_value=answer(choice, local=True))
                    rows = await evaluate([case], [CONFIGS[1]], transport=script, environ={})
                    engine = Engine(model="fake", client=SimpleNamespace(), workdir=Path(directory))
                    engine.history = [{"role": "user", "content": case["task"]}]
                    engine.read_grants.update((engine.workdir / p).resolve() for p in case.get("read_grants", []))
                    app = RockyCodeApp(engine, permission=case["permission_mode"])
                    app._auto_approve.update(case.get("session_tools", []))
                    app._auto_approve_bins.update(case.get("session_binaries", []))
                    app._jev = VerifierGate(VerifierConfig(mode="enforce", provider="ollama"), transport=script)
                    app._add = AsyncMock()
                    app._ask_inline = AsyncMock(return_value="deny")
                    before = script.await_count
                    allowed = await app._approve_tool(case["tool"], case["args"])
                    self.assertEqual(allowed, rows[0]["effective"] == "allow")
                    self.assertEqual(app._ask_inline.await_count, int(rows[0]["effective"] == "ask"))
                    self.assertEqual(script.await_count - before, int(rows[0]["attempted"]))

    async def test_cancellation_is_not_an_approval_result(self):
        with no_execution(), self.assertRaises(asyncio.CancelledError):
            await evaluate([self.by_id["web-explicit"]], CONFIGS,
                           transport=AsyncMock(side_effect=asyncio.CancelledError()), environ=ENV)


class Files(unittest.TestCase):
    def test_case_validation_and_legacy_risk_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.jsonl"
            base = dict(id="legacy", tool="write_file", args={"path": "file.txt"}, task="write file", expected="allow")
            path.write_text(json.dumps(base))
            self.assertEqual(state_for(load_cases(path)[0])["risk"], "moderate")
            for update in ({"risk": "safe"}, {"risk": "risky"}, {"tool": "web_search"}, {"expected_static": "allow"},
                           {"session_tools": True}, {"read_grants": [2]}, {"category": []}):
                path.write_text(json.dumps({**base, **update}))
                with self.subTest(update=update), self.assertRaises(ValueError):
                    load_cases(path)
            path.write_text(json.dumps(base) + '\n' + json.dumps(base))
            with self.assertRaises(ValueError):
                load_cases(path)

    def test_cli_report_schema_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            argv = ["decision_eval", "--cases", str(CORPUS), "--providers", "ollama", "--output", str(output)]
            with patch("sys.argv", argv), patch("rockycode.engine.verifier.DecisionClient._post",
                    new=AsyncMock(return_value=answer(local=True))) as post, redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)  # expected truncated_args guards are not provider failures
                self.assertEqual(post.await_count, 51)
            report = json.loads(output.read_text())
            self.assertEqual(report["schema_version"], 2)
            self.assertEqual(report["policy"]["mode"], "enforce")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            captured = []

            async def post(_self, url, headers, body, timeout_s):
                captured.append((url, headers, body))
                return answer()

            pplx_out = Path(directory) / "pplx.json"
            argv = ["decision_eval", "--cases", str(CORPUS), "--providers", "perplexity", "systemone",
                    "--output", str(pplx_out)]
            with patch.dict("os.environ", {"PERPLEXITY_API_KEY": "pplx-key"}, clear=False), \
                    patch("sys.argv", argv), patch("rockycode.engine.verifier.DecisionClient._post", post), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
            urls = {url for url, _headers, _body in captured}
            self.assertEqual(urls, {
                "https://api.perplexity.ai/v1/decisions",
                "http://127.0.0.1:8092/v1/systemone",
            })
            self.assertTrue(all(body["model"] == "pplx-decider-v1.1-27b" for _url, _headers, body in captured))
            self.assertTrue(all(
                headers.get("Authorization") == "Bearer pplx-key"
                for url, headers, _body in captured if "perplexity.ai" in url))
            self.assertTrue(all(
                "Authorization" not in headers
                for url, headers, _body in captured if url.startswith("http://127.0.0.1")))
            before = output.read_bytes()
            again = ["decision_eval", "--cases", str(CORPUS), "--providers", "ollama", "--output", str(output)]
            with patch("sys.argv", again), patch("rockycode.engine.verifier.DecisionClient._post") as post, \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main()
            post.assert_not_called()
            self.assertEqual(output.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
