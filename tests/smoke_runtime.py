"""Shared Runtime contract: no model/network, real local MCP/LSP subprocesses."""
import asyncio
import gc
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

_GLOBAL = tempfile.TemporaryDirectory(prefix="rocky-runtime-global-")
os.environ["ROCKYCODE_HOME"] = _GLOBAL.name
from rockycode.runtime import build_runtime
from rockycode.engine.mcp import MCPManager, ServerConfig

HERE = Path(__file__).resolve().parent


def client():
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace()), close=AsyncMock())


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rocky-runtime-")
        self.wd = Path(self.temp.name)
        self.runtimes = []

    def make(self, **kw):
        options = dict(model="fake", workdir=self.wd, client=client(), include_user=False, lsp=False)
        options.update(kw)
        runtime = build_runtime(**options)
        self.runtimes.append(runtime)
        return runtime

    async def asyncTearDown(self):
        await asyncio.gather(*(r.close() for r in self.runtimes))
        self.temp.cleanup()

    async def test_composition_and_disabled_features(self):
        (self.wd / "AGENTS.md").write_text("PROJECT_SENTINEL")
        skill = self.wd / ".rockycode/skills/probe/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: probe\ndescription: Runtime probe\n---\nSKILL_SENTINEL")
        rt = self.make()
        self.assertIn("PROJECT_SENTINEL", rt.engine.history[0]["content"])
        self.assertIn("probe", rt.engine.history[0]["content"])
        self.assertIn("SKILL_SENTINEL", await rt.engine.registry["skill"].fn(name="probe"))
        tools = rt.describe()["tools"]
        for name in ("read_file", "write_file", "bash", "skill", "recall_memory", "web_search",
                     "web_research", "web_fetch", "rocky_config", "create_artifact", "explore"):
            self.assertIn(name, tools)
        self.assertNotIn("loop_start", tools)
        for name in tools:
            self.assertIn(name, rt.engine.history[0]["content"])
        off = self.make(skills=False, memory=False, web=False, mcp=False)
        self.assertNotIn("skill", off.engine.registry)
        self.assertNotIn("recall_memory", off.engine.registry)
        self.assertNotIn("web_search", off.engine.registry)
        await rt.close()
        await rt.close()
        rt.engine.client.close.assert_awaited_once()
        self.assertFalse(rt.cleanup_errors)
        with self.assertRaises(RuntimeError):
            await rt.start()

    async def test_cli_uses_same_factory_and_explicit_loop_difference(self):
        from typer.testing import CliRunner
        from rockycode.cli import app
        captured = []

        def build(**kw):
            kw.update(client=client(), include_user=False, lsp=False, mcp=False)
            runtime = build_runtime(**kw)
            captured.append(runtime)
            self.runtimes.append(runtime)
            return runtime

        with patch.dict(os.environ, {"ROCKYCODE_API_KEY": "fixture-home"}), \
             patch("rockycode.tui.app.run_app") as ui, \
             patch("rockycode.runtime.build_runtime", side_effect=build):
            result = CliRunner().invoke(app, ["chat", "--model", "fake", "--workdir", str(self.wd)])
        self.assertEqual(result.exit_code, 0, str(result.exception))
        ui.assert_called_once()
        self.assertEqual(len(captured), 1)
        desktop = self.make(mcp=False)
        extra = set(captured[0].engine.registry) - set(desktop.engine.registry)
        self.assertEqual(extra, {"loop_start", "loop_stop"})
        self.assertFalse(set(desktop.engine.registry) - set(captured[0].engine.registry))

    async def test_mcp_actual_stdio_and_startup_denial(self):
        cfg = ServerConfig("fake", sys.executable, [str(HERE / "fake_mcp_server.py")])
        gate = AsyncMock(return_value=True)
        rt = self.make(startup_approver=gate)
        rt.engine.mcp_manager = MCPManager({"fake": cfg})
        await rt.start()
        await rt.start()
        gate.assert_awaited_once()
        actor = rt.engine.mcp_manager.actors["fake"]
        self.assertEqual(await rt.engine.registry["mcp__fake__add"].fn(a=2, b=3), "5")
        self.assertIn("mcp__fake__add", rt.engine.history[0]["content"])
        await rt.close()
        self.assertTrue(actor._task.done())
        denied = self.make(startup_approver=AsyncMock(return_value=False))
        denied.engine.mcp_manager = MCPManager({"fake": cfg})
        await denied.start()
        self.assertFalse(denied.engine.mcp_manager.actors)
        self.assertNotIn("mcp__fake__add", denied.engine.registry)
        self.assertEqual(next(c for c in denied.describe()["capabilities"] if c["name"] == "mcp")["status"], "failed")

    async def test_tui_closes_the_shared_runtime(self):
        from rockycode.tui.app import RockyCodeApp
        rt = self.make(mcp=False, loops=True)
        app = RockyCodeApp(rt.engine, permission="ask")
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(.1)
        self.assertTrue(rt._closed)
        rt.engine.client.close.assert_awaited_once()
        self.assertFalse(rt.cleanup_errors)

    async def test_cancellation_during_connect_reaps_process(self):
        pid_file = self.wd / "pid"
        code = "import os,time; from pathlib import Path; Path(" + repr(str(pid_file)) + ").write_text(str(os.getpid())); time.sleep(60)"
        rt = self.make()
        rt.engine.mcp_manager = MCPManager({"slow": ServerConfig("slow", sys.executable, ["-c", code])})
        task = asyncio.create_task(rt.start())
        async with asyncio.timeout(10):
            while not pid_file.exists():
                await asyncio.sleep(.02)
        pid = int(pid_file.read_text())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        await rt.close()
        self.assertFalse(rt.engine.mcp_manager._connecting)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        # Regression: the shielded `ready` waiter is gone after the cancel, so the
        # exception close() sets on `ready` has no reader; it must be drained, not
        # logged as "Future exception was never retrieved" at garbage collection.
        loop = asyncio.get_running_loop()
        seen: list[str] = []
        prev = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, ctx: seen.append(str(ctx.get("message", ""))))
        try:
            del rt, task
            gc.collect()
        finally:
            loop.set_exception_handler(prev)
        self.assertFalse([m for m in seen if "never retrieved" in m], seen)

    async def test_lsp_tools_leave_reads_alone_and_clean_up(self):
        with patch("rockycode.engine.lsp.resolve_lsp_config", return_value=(sys.executable, [str(HERE / "fake_lsp_server.py")])):
            rt = self.make(lsp=True)
        (self.wd / "sample.py").write_text("x = 1")
        await rt.start()
        self.assertIsNone(rt.lsp_error)
        self.assertIn("lsp_lookup", rt.engine.registry)
        manager = rt.engine.lsp_manager
        asked, original = [], manager._call

        async def recording(name, arguments):
            asked.append(name)
            return await original(name, arguments)

        manager._call = recording
        # A read is a read: no language-server call rides on it, refused or not.
        output = await rt.engine.registry["read_file"].fn(path="sample.py")
        self.assertIn("x = 1", output)
        self.assertNotIn("diagnostics", output.lower())
        refused = await rt.engine.registry["read_file"].fn(path=".env")
        self.assertTrue(refused.startswith("[blocked]"), refused)
        self.assertEqual(asked, [])
        self.assertEqual(rt.engine.registry["read_file"].risk, "safe")
        # The explicit query still reaches the server.
        self.assertIn("models.py", await rt.engine.registry["lsp_diagnostics"].fn(path=""))
        self.assertEqual(asked, ["diagnostics"])
        task = manager._sessions["default"]._task
        await rt.close()
        self.assertTrue(task.done())
        self.assertIsNone(manager._cleanup_task)
        self.assertFalse(rt.cleanup_errors)

    async def test_lsp_bad_command_line_is_reported_not_launched(self):
        with patch.dict(os.environ, {"ROCKYCODE_LSP_COMMAND": 'python "unclosed'}):
            rt = self.make(lsp=True)
        self.assertIsNone(rt.engine.lsp_manager)
        self.assertIn("not a command line", rt.lsp_error)
        lsp_row = next(row for row in rt.describe()["capabilities"] if row["name"] == "lsp")
        self.assertEqual(lsp_row["status"], "failed")
        self.assertIn("not a command line", lsp_row["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
