"""Offline native-search wire contracts: never calls the network."""
import json
import os
import unittest
from unittest.mock import patch

import httpx
from rockycode.engine import web
from rockycode.engine.providers import Endpoint, ModelSpec, Provider


class NativeSearchTests(unittest.IsolatedAsyncioTestCase):
    def route(self, kind="minimax_messages"):
        if kind == "anthropic":
            p = Provider(name="deepseek", models=["deepseek-flash"], endpoints=[], search=kind)
            return p, p.models[0], "test-deepseek-key", "https://api.deepseek.com/anthropic/v1/messages"
        p = Provider(name="minimax", models=["minimax-m3.1-flash"], endpoints=[], search=kind)
        p.specs[p.models[0]] = ModelSpec(id=p.models[0], provider="minimax", wire="MiniMax-M3.1-Flash", roles=("search",))
        return p, p.models[0], "test-minimax-key", "https://api.minimax.io/anthropic/v1/messages"

    async def run_search(self, responses, kind="minimax_messages", max_uses="12"):
        requests = []
        def handler(req):
            requests.append(req)
            item = responses[len(requests)-1]
            if isinstance(item, Exception):
                raise item
            return httpx.Response(item[0], json=item[1])
        real = httpx.AsyncClient
        client = real(transport=httpx.MockTransport(handler))
        ledger = unittest.mock.Mock()
        self.ledger = ledger
        with patch.object(web, "_native_route", return_value=self.route(kind)), patch.object(httpx, "AsyncClient", return_value=client), \
                patch.dict(os.environ, {"ROCKYCODE_SEARCH_MAX_USES": max_uses}):
            result = await web._native("public docs only", ledger)
        return result, requests, ledger

    async def test_deepseek_success_counts_not_attempts(self):
        _, _, led = await self.run_search([
            (200, {"content": [], "stop_reason": "pause_turn", "usage": {
                "server_tool_use": {"tool_search": 2, "web_search_requests": 7}}}),
            (200, {"content": [], "stop_reason": "end_turn", "usage": {
                "server_tool_use": {"tool_search": 1, "web_search_requests": 5}}}),
        ], kind="anthropic")
        self.assertEqual([c.args[0] for c in led.add_deepseek_search.call_args_list], [2, 1])

    async def test_deepseek_missing_usage_and_provider_isolation(self):
        response = (200, {"content": [], "stop_reason": "end_turn"})
        _, _, led = await self.run_search([response], kind="anthropic")
        self.assertIsNone(led.add_deepseek_search.call_args.args[0])
        _, _, led = await self.run_search([response])
        led.add_deepseek_search.assert_not_called()
        p, model, key, _ = self.route("anthropic")
        with patch.object(self, "route", return_value=(p, model, key, "https://gateway.example/v1/messages")):
            _, _, led = await self.run_search([(200, {"content": [], "stop_reason": "end_turn",
                "usage": {"server_tool_use": {"tool_search": 4}}})], kind="anthropic")
        self.assertIsNone(led.add_deepseek_search.call_args.args[0])

    async def test_deepseek_timeout_is_unknown_and_rejection_not_charged(self):
        with self.assertRaises(httpx.ReadTimeout):
            await self.run_search([httpx.ReadTimeout("timeout")], kind="anthropic")
        self.assertIsNone(self.ledger.add_deepseek_search.call_args.args[0])
        with self.assertRaises(httpx.HTTPStatusError):
            await self.run_search([(401, {"error": "unauthorized"})], kind="anthropic")
        self.ledger.add_deepseek_search.assert_not_called()
        with self.assertRaises(RuntimeError):
            await self.run_search([(200, {"content": [], "stop_reason": "max_tokens",
                "usage": {"server_tool_use": {"tool_search": 2}}})], kind="anthropic")
        self.assertEqual(self.ledger.add_deepseek_search.call_args.args[0], 2)

    def test_deepseek_counter_validation(self):
        for value in (None, True, -1, "2", 1.5):
            self.assertIsNone(web._deepseek_search_count({"usage": {"server_tool_use": {"tool_search": value}}}))
        self.assertEqual(web._deepseek_search_count({"usage": {}, "content": []}), 0)
        self.assertIsNone(web._deepseek_search_count({"usage": {}, "content": [{"type": "server_tool_use"}]}))
        self.assertIsNone(web._deepseek_search_count({"usage": {"server_tool_use": {"web_search_requests": 4}}}))

    async def test_pause_preserves_blocks_sources_usage(self):
        blocks = [{"type":"thinking","thinking":"private","signature":"sig"},
                  {"type":"text","text":"Searching"},
                  {"type":"server_tool_use","id":"s1","name":"web_search","input":{"query":"docs"}},
                  {"type":"web_search_tool_result","content":[{"title":"docs","url":"https://example.com"}]}]
        result, requests, ledger = await self.run_search([
            (200,{"content":blocks,"stop_reason":"pause_turn","usage":{"input_tokens":2}}),
            (200,{"content":[{"type":"text","text":"Final"}],"stop_reason":"end_turn","usage":{"input_tokens":3,"cache_read_input_tokens":4}})])
        self.assertIn("Final",result)
        self.assertIn("https://example.com",result)
        self.assertNotIn("Searching",result)
        self.assertNotIn("private",result)
        body=json.loads(requests[1].content)
        self.assertEqual(body["messages"][1],{"role":"assistant","content":blocks})
        self.assertEqual(body["model"],"MiniMax-M3.1-Flash")
        self.assertEqual(body["tools"],[{"type":"web_search_20250305","name":"web_search","max_uses":12}])
        self.assertEqual(requests[0].headers["x-api-key"],"test-minimax-key")
        self.assertEqual(ledger.add.call_count,2)
        self.assertEqual(ledger.add.call_args.args[1]["prompt_tokens"],7)

    async def test_incomplete_and_errors_fail_closed(self):
        for reason in (None,"max_tokens","tool_use"):
            with self.subTest(reason=reason), self.assertRaises(RuntimeError):
                await self.run_search([(200,{"content":[{"type":"text","text":"partial"}],"stop_reason":reason})])
        for status in (401,403,429,500):
            with self.subTest(status=status), self.assertRaises(httpx.HTTPStatusError):
                await self.run_search([(status,{"error":"failed"})])
        with self.assertRaises(httpx.ReadTimeout):
            await self.run_search([httpx.ReadTimeout("timeout")])
        with self.assertRaises(RuntimeError):
            await self.run_search([(200,{"content":[],"stop_reason":"pause_turn"})]*web.NATIVE_CONTINUATIONS)
        with self.assertRaises(RuntimeError):
            await self.run_search([(200,{"content":[],"stop_reason":"end_turn","base_resp":{"status_code":1000}})])

    async def test_empty_and_deepseek_variant_fallback(self):
        result,_,_=await self.run_search([(200,{"content":[],"stop_reason":"end_turn"})])
        self.assertEqual(result,"")
        result,reqs,_=await self.run_search([(400,{"error":"unknown variant"}),(200,{"content":[{"type":"text","text":"ok"}],"stop_reason":"end_turn"})],kind="anthropic")
        self.assertEqual(result,"ok")
        self.assertEqual(json.loads(reqs[0].content)["tools"][0]["max_uses"],12)
        self.assertEqual(web.SEARCH_MAX_USES,12)
        self.assertEqual(json.loads(reqs[1].content)["tools"][0]["type"],"web_search_20250305")

    async def test_search_max_uses_setting(self):
        from rockycode import config as C
        self.assertEqual(C.DEFAULTS["search_max_uses"],12)
        self.assertIn("search_max_uses",C._ZERO_OK)
        _,reqs,_=await self.run_search([(200,{"content":[{"type":"text","text":"ok"}],"stop_reason":"end_turn"})],max_uses="0")
        self.assertNotIn("max_uses",json.loads(reqs[0].content)["tools"][0])
        _,reqs,_=await self.run_search([(200,{"content":[{"type":"text","text":"ok"}],"stop_reason":"end_turn"})],max_uses="3")
        self.assertEqual(json.loads(reqs[0].content)["tools"][0]["max_uses"],3)
        with patch.dict(os.environ,{},clear=True), patch.object(C,"global_value",return_value=7):
            self.assertEqual(web._search_max_uses(),7)

    def test_registry_selection(self):
        from rockycode.engine import providers as P
        with patch.dict(os.environ, {"ROCKYCODE_SEARCH_MODEL": "minimax:minimax-m3.1-flash", "ROCKYCODE_MINIMAX_API_KEY": "own-key"}, clear=True), patch.object(P, "discover", side_effect=P.catalog):
            provider, model, key, url = web._native_route()
            self.assertEqual(provider.search, "minimax_messages")
            self.assertEqual(model, "minimax-m3.1-flash")
            self.assertEqual(key, "own-key")
            self.assertEqual(url, "https://api.minimax.cn/anthropic/v1/messages")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(web.default_search_order(), ("native", "bing", "duckduckgo"))

    def test_endpoint_key_isolation(self):
        p,model,_,_=self.route()
        ep=Endpoint("minimax-custom","https://api.minimax.io/v1","ROCKYCODE_MINIMAX_API_KEY")
        with patch.dict(os.environ,{"ROCKYCODE_MINIMAX_API_KEY":"own-key","ROCKYCODE_API_KEY":"home-key"},clear=True), patch("rockycode.engine.providers.resolve",return_value=(p,ep,model)):
            self.assertEqual(web._native_route()[2:],("own-key","https://api.minimax.io/anthropic/v1/messages"))
        with patch.dict(os.environ,{"ROCKYCODE_API_KEY":"home-key"},clear=True), patch("rockycode.engine.providers.resolve",return_value=(p,ep,model)), self.assertRaises(RuntimeError):
            web._native_route()
        p.search=""
        with patch("rockycode.engine.providers.resolve",return_value=(p,ep,model)), self.assertRaises(ValueError):
            web._native_route()


if __name__ == "__main__":
    unittest.main()
