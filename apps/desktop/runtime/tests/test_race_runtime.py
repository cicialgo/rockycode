"""Offline confinement tests, plus real upstream Engine with fake transports."""
import json
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rockycode_desktop import race_runtime as race

try:
    from rockycode.engine.providers import Choice, Endpoint, Provider, ModelSpec
    from rockycode.runtime import build_runtime
except ImportError:
    build_runtime = None


def choice(key="configured-test-key", *, local=False, priced=False):
    endpoint = Endpoint("test-endpoint", "http://127.0.0.1:9000/v1", "" if local else "ROCKYCODE_FAKE_TEST_KEY")
    endpoint.key = lambda: key
    provider = Provider("test-provider", ["test-model"], [endpoint], local=local, reasoning="none")
    provider.specs["test-model"] = ModelSpec("test-model", "test-provider", context=100_000)
    return Choice(provider, endpoint, "test-model")


def fake_client(replies, *, usage=True):
    calls, iterator = [], iter(replies)

    async def create(**kwargs):
        calls.append(kwargs)
        reply = next(iterator)
        delta = NS(content=reply if isinstance(reply, str) else None, reasoning_content=None, tool_calls=None)
        if isinstance(reply, tuple):
            name, args = reply
            delta.tool_calls = [NS(index=0, id="call-" + str(len(calls)), function=NS(name=name, arguments=json.dumps(args)))]

        async def stream():
            yield NS(usage=None, choices=[NS(delta=delta,
                finish_reason="tool_calls" if delta.tool_calls else "stop")])
            if usage:
                yield NS(usage={"prompt_tokens": 10, "completion_tokens": 5}, choices=[])
        return stream()
    return NS(chat=NS(completions=NS(create=create)), close=AsyncMock(), calls=calls)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rocky-race-tests-")
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.destination = self.root / "snapshot"

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self):
        return race.snapshot_workspaces(self.source, self.destination)

    def test_empty_five_separate_workspaces(self):
        result = race.snapshot_workspaces(None, self.destination)
        self.assertEqual(len(set(result["workspaces"])), 5)
        self.assertEqual(result["includedFiles"], 0)
        self.assertEqual(len(result["snapshotHash"]), 64)
        self.assertTrue(all(Path(path).is_dir() for path in result["workspaces"]))

    def test_source_frozen_once_and_files_not_linked(self):
        (self.source / "nested").mkdir()
        (self.source / "nested" / "hello.txt").write_text("original")
        result = self.snapshot()
        (self.source / "nested" / "hello.txt").write_text("changed live source")
        paths = [Path(p) / "nested" / "hello.txt" for p in result["workspaces"]]
        self.assertTrue(all(p.read_text() == "original" for p in paths))
        self.assertEqual(len({p.stat().st_ino for p in paths}), 5)
        paths[0].write_text("lane one")
        self.assertTrue(all(p.read_text() == "original" for p in paths[1:]))
        self.assertEqual(result["includedFiles"], 1)

    def test_deterministic_hash_covers_paths_and_bytes(self):
        (self.source / "a.txt").write_text("same")
        first = self.snapshot()
        second = race.snapshot_workspaces(self.source, self.root / "second")
        self.assertEqual(first["snapshotHash"], second["snapshotHash"])
        (self.source / "a.txt").rename(self.source / "b.txt")
        third = race.snapshot_workspaces(self.source, self.root / "third")
        self.assertNotEqual(first["snapshotHash"], third["snapshotHash"])

    def test_excludes_configs_credentials_and_caches(self):
        for name in (".git", ".rockycode", "node_modules"):
            (self.source / name).mkdir()
            (self.source / name / "hidden.txt").write_text("must not copy")
        for name in (".env", "private.key", "credentials.json", "access-token.txt"):
            (self.source / name).write_text("must not copy")
        (self.source / "task.txt").write_text("allowed")
        result = self.snapshot()
        self.assertEqual(result["includedFiles"], 1)
        self.assertEqual(result["excludedFiles"], 7)
        self.assertEqual(result["manifest"]["included"], ["task.txt"])

    def test_symlink_refused(self):
        (self.source / "alias").symlink_to(self.root / "outside")
        with self.assertRaises(ValueError):
            self.snapshot()
        self.assertFalse(self.destination.exists())

    def test_symlink_ancestor_refused(self):
        linked = self.root / "linked"
        linked.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(OSError):
            race.snapshot_workspaces(linked, self.destination)

    def test_hardlink_refused(self):
        original = self.root / "outside.txt"
        original.write_text("outside")
        os.link(original, self.source / "hardlink.txt")
        with self.assertRaises(ValueError):
            self.snapshot()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX only")
    def test_special_file_refused_without_blocking(self):
        os.mkfifo(self.source / "pipe")
        with self.assertRaises(ValueError):
            self.snapshot()

    def test_binary_refused(self):
        (self.source / "binary.txt").write_bytes(b"abc\x00def")
        with self.assertRaises(ValueError):
            self.snapshot()

    def test_size_and_count_limits_refuse_whole_snapshot(self):
        (self.source / "large.txt").write_bytes(b"x" * (race.MAX_FILE_BYTES + 1))
        with self.assertRaises(ValueError):
            self.snapshot()
        self.assertFalse(self.destination.exists())
        (self.source / "large.txt").unlink()
        for i in range(race.MAX_FILES + 1):
            (self.source / f"file-{i}.txt").write_text("small")
        with self.assertRaises(ValueError):
            self.snapshot()
        self.assertFalse(self.destination.exists())

    def test_destination_swap_cannot_create_or_overwrite_source_files(self):
        (self.source / "input.txt").write_text("original source")
        moved = self.root / "moved-snapshot"
        real_mkdir = os.mkdir
        swapped = False

        def swap_before_lane(path, *args, **kwargs):
            nonlocal swapped
            if path == "lane-1" and not swapped:
                swapped = True
                self.destination.rename(moved)
                self.destination.symlink_to(self.source, target_is_directory=True)
            return real_mkdir(path, *args, **kwargs)

        with patch.object(race.os, "mkdir", side_effect=swap_before_lane):
            with self.assertRaises((OSError, ValueError)):
                self.snapshot()
        self.assertTrue(swapped)
        self.assertEqual((self.source / "input.txt").read_text(), "original source")
        self.assertEqual(sorted(p.name for p in self.source.iterdir()), ["input.txt"])
        self.assertTrue(self.destination.is_symlink())

    def test_existing_destination_never_changed(self):
        self.destination.mkdir()
        keep = self.destination / "keep.txt"
        keep.write_text("keep")
        with self.assertRaises(FileExistsError):
            self.snapshot()
        self.assertEqual(keep.read_text(), "keep")

    def test_private_workspace_refuses_aliased_parent_before_creating_anything(self):
        parent = self.root / "judge-parent"
        parent.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(OSError):
            race.create_private_workspace(parent, "judge-unique")
        self.assertEqual(list(self.source.iterdir()), [])

    def test_private_workspace_checks_parent_swap_before_return(self):
        parent = self.root / "judge-parent"
        parent.mkdir()
        real_mkdir = os.mkdir

        def swap_after_child(path, *args, **kwargs):
            result = real_mkdir(path, *args, **kwargs)
            if path == "judge-unique":
                parent.rename(self.root / "saved-parent")
                parent.symlink_to(self.source, target_is_directory=True)
            return result

        with patch.object(race.os, "mkdir", side_effect=swap_after_child):
            with self.assertRaises(OSError):
                race.create_private_workspace(parent, "judge-unique")
        self.assertEqual(list(self.source.iterdir()), [])
        self.assertEqual(list((self.root / "saved-parent").iterdir()), [])

    def test_private_workspace_is_new_and_bounded_to_one_name(self):
        child = race.create_private_workspace(self.source, "judge-unique")
        self.assertEqual(Path(child), self.source / "judge-unique")
        with self.assertRaises(FileExistsError):
            race.create_private_workspace(self.source, "judge-unique")
        with self.assertRaises(ValueError):
            race.create_private_workspace(self.source, "../elsewhere")

    def test_result_evidence_truncation_is_explicit_and_links_rejected(self):
        (self.source / "large.txt").write_text("x" * 120_000)
        result = race.result_files(self.source)
        self.assertEqual(len(result[0]["content"]), 100_000)
        self.assertTrue(result[0]["truncated"])
        (self.source / "linked.txt").symlink_to(self.root / "outside")
        with self.assertRaises(ValueError):
            race.result_files(self.source)

    def test_total_bytes_depth_and_visited_entry_limits(self):
        for i in range(9):
            (self.source / f"file-{i}.txt").write_bytes(b"x" * race.MAX_FILE_BYTES)
        with self.assertRaisesRegex(ValueError, "total limit"):
            self.snapshot()
        self.assertFalse(self.destination.exists())
        for file in self.source.iterdir():
            file.unlink()
        deep = self.source
        for i in range(race.MAX_DEPTH + 1):
            deep = deep / "nested"
            deep.mkdir()
        with self.assertRaisesRegex(ValueError, "too deep"):
            self.snapshot()

    def test_source_destination_overlap_refused(self):
        with self.assertRaises(ValueError):
            race.snapshot_workspaces(self.source, self.source / "snapshot")
        with self.assertRaises(ValueError):
            race.snapshot_workspaces(self.source, self.root)


class FilesOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rocky-race-tools-")
        self.root = Path(self.temp.name).resolve()
        self.lane = self.root / "lane"
        self.lane.mkdir()
        self.tools = race._Workspace(self.lane)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_read_write_and_list(self):
        await self.tools.write("nested/result.txt", "one\ntwo\n")
        self.assertEqual(await self.tools.read("nested/result.txt", offset=2), "2: two")
        self.assertEqual(await self.tools.listing(), "nested/")
        self.assertEqual(await self.tools.listing("nested"), "result.txt")
        await self.tools.write("nested/result.txt", "replacement")
        self.assertIn("replacement", await self.tools.read("nested/result.txt"))

    async def test_paths_cannot_escape_or_access_configuration(self):
        for path in ("../outside", "/etc/passwd", "C:/Users/a", "C:\\Users\\a", "a/../x", "a//x", ".env", ".rockycode/settings", "secret.txt", "./a"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    await self.tools.write(path, "bad")
                with self.assertRaises(ValueError):
                    await self.tools.read(path)

    async def test_symlink_read_write_and_directory_escape_denied(self):
        outside = self.root / "outside.txt"
        outside.write_text("private")
        (self.lane / "link.txt").symlink_to(outside)
        with self.assertRaises((ValueError, OSError)):
            await self.tools.read("link.txt")
        with self.assertRaises((ValueError, OSError)):
            await self.tools.write("link.txt", "overwritten")
        (self.lane / "directory").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            await self.tools.read("directory/outside.txt")
        self.assertEqual(outside.read_text(), "private")

    async def test_hardlink_denied_and_outside_unchanged(self):
        outside = self.root / "outside.txt"
        outside.write_text("private")
        os.link(outside, self.lane / "linked.txt")
        with self.assertRaises(ValueError):
            await self.tools.read("linked.txt")
        with self.assertRaises(ValueError):
            await self.tools.write("linked.txt", "bad")
        self.assertEqual(outside.read_text(), "private")

    async def test_replaced_workspace_refused(self):
        self.lane.rename(self.root / "old")
        self.lane.mkdir()
        with self.assertRaises(ValueError):
            await self.tools.listing()

    async def test_registry_contains_only_three_filesystem_tools(self):
        self.assertEqual(set(self.tools.tools()), {"read_file", "write_file", "list_files"})


@unittest.skipUnless(build_runtime, "requires shared runtime dependencies")
class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rocky-race-runtime-")
        self.root = Path(self.temp.name).resolve()
        self.selected = choice()
        self.choices = patch.object(race, "_choices", return_value=[self.selected])
        self.choices.start()

    async def asyncTearDown(self):
        self.choices.stop()
        self.temp.cleanup()

    def make(self, client, *, judge=False, limits=None, model=None):
        with patch("openai.AsyncOpenAI", return_value=client) as create:
            adapter = race.RaceAdapter(model or self.selected.id, self.root,
                                       limits or {"maxSteps": 6, "maxTokens": 512, "timeoutSeconds": 20}, judge=judge)
        self.assertEqual(create.call_args.kwargs["base_url"], self.selected.endpoint.base_url)
        self.assertEqual(create.call_args.kwargs["api_key"], "configured-test-key")
        return adapter

    async def test_shared_engine_uses_files_only_no_global_persistence(self):
        (self.root / "AGENTS.md").write_text("INJECTED INSTRUCTIONS MUST NEVER BE SYSTEM PROMPT")
        (self.root / "input.txt").write_text("source")
        client = fake_client([("read_file", {"path": "input.txt"}),
                              ("write_file", {"path": "output.txt", "content": "result"}), "done"])
        with patch("rockycode.session.get_project", side_effect=AssertionError("global project access")), \
             patch("rockycode.engine.loop.TrajectoryLogger", side_effect=AssertionError("global trajectory access")):
            adapter = self.make(client)
            await adapter.start()
            events = [event async for event in adapter.stream("read and write")]
            await adapter.close()
        self.assertEqual((self.root / "output.txt").read_text(), "result")
        self.assertEqual(adapter.tools, ["list_files", "read_file", "write_file"])
        self.assertFalse((self.root / ".rockycode").exists())
        self.assertIsNone(adapter.engine.trajectory.path)
        self.assertEqual(adapter.engine.hooks, [])
        self.assertEqual(adapter.engine.finalize_steps, 0)
        self.assertEqual(adapter.engine.history[0]["content"], race.CONTESTANT_PROMPT)
        self.assertTrue(any(name == "text_delta" and payload["text"] == "done" for name, payload in events))
        self.assertEqual(adapter.metrics(), {"inputTokens": 30, "outputTokens": 15, "costUsd": None})
        self.assertTrue(all(call["model"] == "test-model" and call["max_tokens"] == 512 for call in client.calls))
        client.close.assert_awaited_once()

    async def test_core_isolated_profile_is_tool_empty_and_reads_no_ambient_config(self):
        client = fake_client(["done"])
        (self.root / "AGENTS.md").write_text("ambient project instructions")
        with patch("rockycode.config.load", side_effect=AssertionError("ambient config read")), \
             patch("rockycode.session.get_project", side_effect=AssertionError("global project write")), \
             patch("rockycode.engine.tools.build_registry", side_effect=AssertionError("global tool assembly")), \
             patch("rockycode.engine.loop.TrajectoryLogger", side_effect=AssertionError("global trajectory")):
            runtime = build_runtime(model="fake", workdir=self.root, client=client,
                                    system_prompt="fixed contract", isolated=True)
            await runtime.start()
            self.assertEqual(runtime.engine.registry, {})
            self.assertEqual(runtime.engine.history[0]["content"], "fixed contract")
            self.assertFalse(any(runtime.features.values()))
            self.assertEqual(runtime.engine.hooks, [])
            runtime.refresh_prompt()
            self.assertEqual(runtime.engine.history[0]["content"], "fixed contract")
            await runtime.close()

    async def test_contestant_and_judge_preserve_selected_wire_id(self):
        from rockycode.engine.providers import catalog

        minimax = catalog()["minimax"]
        official = Choice(minimax, self.selected.endpoint, "minimax-m3.1-flash")
        custom = choice()
        custom.spec.wire = "Lab/Model-20261022"
        for selected, expected in ((official, "MiniMax-M3.1-Flash"),
                                   (custom, "Lab/Model-20261022")):
            for judge in (False, True):
                with self.subTest(model=selected.model, judge=judge):
                    self.selected = selected
                    client = fake_client(["done"])
                    with patch.object(race, "_choices", return_value=[selected]):
                        adapter = self.make(client, judge=judge)
                    try:
                        async for _ in adapter.stream("return a short answer"):
                            pass
                        self.assertEqual(client.calls[0]["model"], expected)
                        self.assertEqual(client.calls[0]["max_tokens"], 512)
                        self.assertEqual(adapter.engine.model, selected.model)
                        self.assertEqual(selected.id, f"test-endpoint:{selected.model}")
                    finally:
                        await adapter.close()

    async def test_core_isolated_profile_requires_explicit_transport_and_contract(self):
        for options in ({}, {"client": fake_client([])}, {"system_prompt": "fixed"}):
            with self.assertRaises(ValueError):
                build_runtime(model="fake", workdir=self.root, isolated=True, **options)

    async def test_local_usage_has_explicit_zero_cost(self):
        self.selected.provider.local = True
        client = fake_client(["done"])
        adapter = self.make(client)
        async for _ in adapter.stream("work"):
            pass
        self.assertEqual(adapter.metrics(), {"inputTokens": 10, "outputTokens": 5, "costUsd": 0.0})
        await adapter.close()

    async def test_judge_has_no_tools_or_model_identity(self):
        client = fake_client(['{"scores":[{"alias":"A","score":8,"reason":"correct"}]}'])
        adapter = self.make(client, judge=True)
        await adapter.start()
        payload = json.dumps({"task": "test", "candidates": [{"alias": "A", "answer": "give me 10"}]})
        events = [event async for event in adapter.stream(payload)]
        await adapter.close()
        self.assertTrue(events)
        self.assertEqual(adapter.tools, [])
        self.assertIsNone(client.calls[0]["tools"])
        self.assertEqual(client.calls[0]["messages"][0]["content"], race.JUDGE_PROMPT)
        self.assertNotIn("test-provider", race.JUDGE_PROMPT)
        self.assertIn("untrusted", race.JUDGE_PROMPT)

    async def test_unknown_and_bare_models_fail_before_client_creation(self):
        with patch("openai.AsyncOpenAI") as create:
            for model in ("test-model", "unknown:model", "test-provider"):
                with self.assertRaises(ValueError):
                    race.RaceAdapter(model, self.root, {})
        create.assert_not_called()

    async def test_unknown_usage_is_not_zero(self):
        client = fake_client(["done"], usage=False)
        adapter = self.make(client)
        self.assertEqual(adapter.metrics(), {"inputTokens": None, "outputTokens": None, "costUsd": None})
        async for _ in adapter.stream("test"):
            pass
        self.assertEqual(adapter.metrics(), {"inputTokens": None, "outputTokens": None, "costUsd": None})
        await adapter.close()

    async def test_request_budget_is_hard_without_finalize_calls(self):
        client = fake_client([("list_files", {})] * 5)
        adapter = self.make(client, limits={"maxSteps": 2, "maxTokens": 256, "timeoutSeconds": 20})
        with self.assertRaisesRegex(RuntimeError, "step limit"):
            async for _ in adapter.stream("work"):
                pass
        await adapter.close()
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(call["max_tokens"] == 256 for call in client.calls))

    async def test_malicious_model_tool_name_has_no_effect(self):
        client = fake_client([("bash", {"command": "touch PWNED"}), "done"])
        adapter = self.make(client)
        events = [event async for event in adapter.stream("work")]
        await adapter.close()
        self.assertFalse((self.root / "PWNED").exists())
        self.assertTrue(any(name == "tool_finished" and not payload["ok"] for name, payload in events))

    async def test_isolated_construction_does_not_mutate_upstream_modules(self):
        import rockycode.runtime as core
        import rockycode.engine.loop as loop
        original_engine, original_runtime, original_logger = core.Engine, core.Runtime, loop.TrajectoryLogger
        client = fake_client(["done"])
        adapter = self.make(client)
        self.assertIs(core.Engine, original_engine)
        self.assertIs(core.Runtime, original_runtime)
        self.assertIs(loop.TrajectoryLogger, original_logger)
        await adapter.close()


@unittest.skipUnless(build_runtime, "requires shared runtime dependencies")
class ConfiguredModelTests(unittest.TestCase):
    def setUp(self):
        race._capability_cache.clear()

    def tearDown(self):
        race._capability_cache.clear()

    def test_unkeyed_default_and_untrusted_keyless_are_not_offered(self):
        good, unkeyed, unsafe = choice(), choice(key=None), choice(local=False)
        unsafe.endpoint.key_env = ""
        with patch("rockycode.onboarding.bootstrap_credentials"), \
             patch("rockycode.engine.providers.configured_choices", return_value=[good, unkeyed, unsafe]):
            self.assertEqual(race.configured_models(), [{"id": good.id, "label": good.model,
                                                        "provider": good.provider.name, "model": good.model}])

    def test_local_embedding_and_non_tool_models_are_not_selectable(self):
        local = choice(local=True)
        local.provider.name = 'ollama'
        cloud = choice()
        for capabilities, offered in ((['embedding'], False), (['completion'], False),
                                      (['tools'], False), (['completion', 'tools'], True)):
            with self.subTest(capabilities=capabilities), \
                 patch('rockycode.onboarding.bootstrap_credentials'), \
                 patch('rockycode.engine.providers.configured_choices', return_value=[cloud, local]), \
                 patch.object(race, '_local_capabilities', return_value=frozenset(capabilities)):
                self.assertEqual([model['id'] for model in race.configured_models()],
                                 [cloud.id, local.id] if offered else [cloud.id])

    def test_unverified_ollama_is_hidden_but_other_compatible_servers_remain_available(self):
        for provider, offered in (('ollama', False), ('custom-local', True)):
            local = choice(local=True)
            local.provider.name = provider
            with self.subTest(provider=provider), \
                 patch('rockycode.onboarding.bootstrap_credentials'), \
                 patch('rockycode.engine.providers.configured_choices', return_value=[local]), \
                 patch.object(race, '_local_capabilities', return_value=None):
                self.assertEqual(bool(race.configured_models()), offered)

    def test_provider_without_native_tools_is_hidden_without_probing(self):
        local = choice(local=True)
        local.provider.tools = 'off'
        with patch('rockycode.onboarding.bootstrap_credentials'), \
             patch('rockycode.engine.providers.configured_choices', return_value=[local]), \
             patch.object(race, '_local_capabilities') as probe:
            self.assertEqual(race.configured_models(), [])
        probe.assert_not_called()

    def test_capability_metadata_uses_wire_id_and_caches_without_model_execution(self):
        local = choice(local=True)
        local.spec.wire = 'Local/Wire-Model'
        with patch('rockycode.onboarding.bootstrap_credentials'), \
             patch('rockycode.engine.providers.configured_choices', return_value=[local]), \
             patch.object(race.urllib.request, 'urlopen', return_value=io.BytesIO(
                 b'{"capabilities":["completion","tools"]}')) as request:
            for _ in range(2):
                self.assertEqual(len(race.configured_models()), 1)
        request.assert_called_once()
        posted = request.call_args.args[0]
        self.assertEqual(posted.full_url, 'http://127.0.0.1:9000/api/show')
        self.assertEqual(json.loads(posted.data), {'model': 'Local/Wire-Model'})
        self.assertNotIn('Authorization', posted.headers)

    def test_failed_or_malformed_metadata_recovers_after_short_cache_expiry(self):
        base = 'http://127.0.0.1:9000/v1'
        for body in (b'[]', b'{"capabilities":"completion"}', b'{"capabilities":[]}',
                     b'{"capabilities":[1]}', b'not json'):
            race._capability_cache.clear()
            with self.subTest(body=body), patch.object(race.time, 'monotonic', return_value=10), \
                 patch.object(race.urllib.request, 'urlopen', return_value=io.BytesIO(body)):
                self.assertIsNone(race._local_capabilities(base, 'model'))
        race._capability_cache.clear()
        with patch.object(race.time, 'monotonic', return_value=10), \
             patch.object(race.urllib.request, 'urlopen', side_effect=OSError('offline')):
            self.assertIsNone(race._local_capabilities(base, 'model'))
        with patch.object(race.time, 'monotonic', return_value=14), \
             patch.object(race.urllib.request, 'urlopen') as request:
            self.assertIsNone(race._local_capabilities(base, 'model'))
            request.assert_not_called()
        with patch.object(race.time, 'monotonic', return_value=16), \
             patch.object(race.urllib.request, 'urlopen', return_value=io.BytesIO(
                 b'{"capabilities":["completion","tools"]}')):
            self.assertEqual(race._local_capabilities(base, 'model'), {'completion', 'tools'})

    def test_unsupported_model_is_rejected_for_contestant_and_judge_before_client_creation(self):
        local = choice(local=True)
        local.provider.name = 'ollama'
        with tempfile.TemporaryDirectory() as workspace, \
             patch('rockycode.onboarding.bootstrap_credentials'), \
             patch('rockycode.engine.providers.configured_choices', return_value=[local]), \
             patch.object(race, '_local_capabilities', return_value=frozenset({'embedding'})), \
             patch('openai.AsyncOpenAI') as client:
            for judge in (False, True):
                with self.subTest(judge=judge), self.assertRaisesRegex(ValueError, 'exact configured'):
                    race.RaceAdapter(local.id, Path(workspace).resolve(), {}, judge=judge)
        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
