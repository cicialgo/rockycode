"""Offline positive/negative tests for bounded and local YOLO file reads."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from rockycode.engine.file_access import FileAccessPolicy
from rockycode.headless import host_profile_registry
from rockycode.engine.permission import decide
from rockycode.engine.tools import build_registry, execute


class FileAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rocky-file-access-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / "workspace"
        self.out = self.root / "reference"
        self.work.mkdir()
        self.out.mkdir()
        (self.work / "local.txt").write_text("LOCAL_MARKER")
        (self.out / "note.txt").write_text("OUTSIDE_MARKER")
        (self.out / "picture.png").write_bytes(b"test image, never decoded")
        (self.out / ".env").write_text("SYNTHETIC_SECRET_MARKER")
        (self.work / "link").symlink_to(self.out, target_is_directory=True)
        self.mode = "ask"
        self.access = FileAccessPolicy(self.work, local_mode=lambda: self.mode)
        self.reg = build_registry(self.work, file_access=self.access, vision_active=lambda: True)

    async def call(self, name, **args):
        return await execute(self.reg, name, json.dumps(args))

    async def test_bounded_default_and_headless_profiles(self):
        for reg in (build_registry(self.work), host_profile_registry(self.work, profile="read"),
                    host_profile_registry(self.work, profile="write")):
            for name, args in (
                ("read_file", {"path": str(self.out / "note.txt")}),
                ("grep", {"pattern": "MARKER", "path": str(self.out)}),
                ("glob", {"pattern": "../reference/*.txt"}),
                ("view_image", {"path": str(self.out / "picture.png")}),
            ):
                text, _ = await execute(reg, name, json.dumps(args))
                self.assertIn("[blocked]", text, (name, text))

    async def test_yolo_reads_and_live_mode_change_do_not_grant_writes(self):
        self.mode = "yolo"
        for name, args in (
            ("read_file", {"path": str(self.out / "note.txt")}),
            ("grep", {"pattern": "OUTSIDE", "path": str(self.out)}),
        ):
            self.assertIn("OUTSIDE_MARKER", (await self.call(name, **args))[0])
        self.assertIn("note.txt", (await self.call("glob", pattern=str(self.out / "*.txt")))[0])
        with patch("rockycode.engine.images.downscale_for_api", side_effect=lambda p: p):
            self.assertIn("[image-attach]", (await self.call("view_image", path=str(self.out / "picture.png")))[0])
        self.assertIn("[blocked]", (await self.call("write_file", path=str(self.out / "new.txt"), content="bad"))[0])
        self.assertFalse((self.out / "new.txt").exists())
        separate = host_profile_registry(self.work, profile="read")
        text, _ = await execute(separate, "grep", json.dumps({"path": str(self.out), "pattern": "OUTSIDE"}))
        self.assertIn("[blocked]", text, "local YOLO leaked into a separate headless registry")
        self.mode = "careful"
        self.assertIn("[blocked]", (await self.call("grep", pattern="OUTSIDE", path=str(self.out)))[0])
        self.assertIn("[blocked]", (await self.call("read_file", path=str(self.out / "note.txt")))[0])

    async def test_read_grants_and_explicit_roots_are_shared(self):
        self.access.grant_read("glob", {"pattern": str(self.out / "*.txt")})
        self.assertIn(self.out, self.access.read_grants)
        self.assertEqual(decide("ask", "safe", {"pattern": str(self.out / "*.txt")}, self.work,
                                tool="glob", read_grants=self.access.read_grants), "allow")
        self.assertIn("OUTSIDE", (await self.call("grep", pattern="OUTSIDE", path=str(self.out)))[0])
        self.assertIn("OUTSIDE", (await self.call("read_file", path=str(self.out / "note.txt")))[0])
        self.assertIn("[blocked]", (await self.call("edit_file", path=str(self.out / "note.txt"),
                                                  old_string="OUTSIDE", new_string="bad"))[0])
        reg = build_registry(self.work, allowed_roots=(self.out,))
        for name, args in (("grep", {"pattern": "OUTSIDE", "path": str(self.out)}),
                           ("glob", {"pattern": str(self.out / "*.txt")})):
            text, _ = await execute(reg, name, json.dumps(args))
            self.assertIn("note.txt", text)

    async def test_file_target_tilde_and_double_dots_in_names(self):
        self.mode = "yolo"
        self.assertIn("note.txt:1: OUTSIDE", (await self.call("grep", pattern="OUTSIDE", path=str(self.out / "note.txt")))[0])
        with patch("os.path.expanduser", side_effect=lambda p: str(self.root) + p[1:]):
            self.assertIn("OUTSIDE", (await self.call("read_file", path="~/reference/note.txt"))[0])
        self.assertEqual(decide("ask", "safe", {"pattern": "release..notes*.txt"}, self.work, tool="glob"), "allow")

    async def test_traversal_checks_before_enumeration_and_symlink_cycles(self):
        (self.work / "cycle").symlink_to(self.work, target_is_directory=True)
        real_iterdir = Path.iterdir

        def guarded(path):
            self.assertNotEqual(path.resolve(), self.out, "enumerated an unauthorized directory")
            return real_iterdir(path)

        with patch.object(Path, "iterdir", guarded):
            for name, args in (("grep", {"pattern": "MARKER"}), ("glob", {"pattern": "**/*.txt"}),
                               ("glob", {"pattern": "l*/*.txt"})):
                text, _ = await asyncio.wait_for(self.call(name, **args), 2)
                self.assertNotIn("OUTSIDE", text)
                self.assertNotIn("note.txt", text)
        self.mode = "yolo"
        self.assertIn("OUTSIDE", (await asyncio.wait_for(self.call("grep", pattern="MARKER"), 2))[0])

    async def test_secret_filters_apply_to_search_and_image_paths(self):
        self.mode = "yolo"
        (self.out / "alias.txt").symlink_to(self.out / ".env")
        for name, args in (("read_file", {"path": str(self.out / "alias.txt")}),
                           ("grep", {"pattern": "SECRET", "path": str(self.out)}),
                           ("glob", {"pattern": str(self.out / "*")})):
            text, _ = await self.call(name, **args)
            self.assertNotIn("SYNTHETIC_SECRET_MARKER", text)
            if name == "glob":
                self.assertNotIn(".env", text)
                self.assertNotIn("alias.txt", text)

    async def test_only_trusted_image_backend_config_is_loaded(self):
        self.mode = "yolo"
        reg = build_registry(self.work, file_access=self.access)
        with patch("rockycode.config.load", return_value={"image": "trusted-image {path}"}) as config, \
             patch("rockycode.engine.vision.describe", new_callable=AsyncMock, return_value=("description", "trusted")) as describe:
            text, _ = await execute(reg, "view_image", json.dumps({"path": str(self.out / "picture.png")}))
            self.assertIn("description", text)
            config.assert_called_once_with()
            self.assertEqual(describe.await_args.kwargs["template"], "trusted-image {path}")

    async def test_readonly_children_inherit_scope_without_mutation_tools(self):
        from rockycode.engine.explore import build_explore_registry
        reg = build_explore_registry(self.work, file_access=self.access)
        self.assertNotIn("write_file", reg)
        self.assertNotIn("edit_file", reg)
        self.mode = "yolo"
        args = json.dumps({"pattern": "OUTSIDE", "path": str(self.out)})
        self.assertIn("OUTSIDE", (await execute(reg, "grep", args))[0])
        self.mode = "ask"
        self.assertIn("[blocked]", (await execute(reg, "grep", args))[0])
        self.access.read_grants.add(self.out)
        self.assertIn("OUTSIDE", (await execute(reg, "grep", args))[0])

    async def test_tui_mode_and_read_approval_use_live_policy(self):
        from rockycode.engine.loop import Engine
        from rockycode.engine.trajectory import TrajectoryLogger
        from rockycode.tui.app import RockyCodeApp
        engine = Engine(model="fake", client=NS(), workdir=self.work,
                        trajectory_logger=TrajectoryLogger({}, enabled=False))
        app = RockyCodeApp(engine, permission="yolo", evolve="off")
        text, _ = await execute(engine.registry, "grep", json.dumps({"pattern": "OUTSIDE", "path": str(self.out)}))
        self.assertIn("OUTSIDE", text)
        app._grant_read_if_escaping("glob", {"pattern": str(self.out / "*.txt")})
        self.assertIn(self.out, engine.read_grants)
        with patch.object(app, "_render_cwd"):
            app._set_permission_mode("ask")
        self.assertFalse(engine.read_grants)
        text, _ = await execute(engine.registry, "glob", json.dumps({"pattern": str(self.out / "*.txt")}))
        self.assertIn("[blocked]", text)

    async def test_container_read_diagnostics_treat_paths_as_data(self):
        from rockycode.engine import container, sandbox

        class LocalTransport:
            async def exec(inner, command):
                proc = await asyncio.create_subprocess_exec("sh", "-c", command, cwd=self.work,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                data, _ = await proc.communicate()
                return data.decode(), proc.returncode

        for module in (container, sandbox):
            for path in ('missing$(touch INJECTION_MARKER)', '-literal', 'directory$(touch INJECTION_MARKER)'):
                if path.startswith('directory'):
                    (self.work / path).mkdir(exist_ok=True)
                result = await module._read_file(LocalTransport(), path)
                self.assertIn(path, result)
                self.assertFalse((self.work / "INJECTION_MARKER").exists())
                self.assertNotIn('\\n', result)

    async def test_live_sandbox_swap_clears_and_restores_local_policy(self):
        import inspect
        from rockycode.engine.loop import Engine
        from rockycode.engine.trajectory import TrajectoryLogger
        from rockycode.tui.app import RockyCodeApp
        engine = Engine(model="fake", client=NS(), workdir=self.work,
                        trajectory_logger=TrajectoryLogger({}, enabled=False))
        app = RockyCodeApp(engine, permission="yolo", evolve="off")
        app._local_registry = dict(engine.registry)  # normally captured on mount
        local_policy = engine.file_access
        sandbox = NS(is_running=True, container_id="fake", stop=AsyncMock())
        handler = inspect.unwrap(RockyCodeApp._handle_sandbox)
        with patch("rockycode.engine.sandbox.ChatSandbox.start", AsyncMock(return_value=sandbox)), \
             patch.object(app, "_add", AsyncMock()), patch.object(app, "_render_status"), \
             patch.object(app, "_maybe_nudge_yolo_host", AsyncMock()):
            await handler(app, "/sandbox on")
            self.assertIsNone(engine.file_access)
            self.assertNotIn("explore", engine.registry)
            await handler(app, "/sandbox off")
        self.assertIs(engine.file_access, local_policy)
        text, _ = await execute(engine.registry, "grep", json.dumps({"pattern": "OUTSIDE", "path": str(self.out)}))
        self.assertIn("OUTSIDE", text)
        sandbox.stop.assert_awaited_once()

    async def test_custom_registry_never_inherits_local_yolo(self):
        from rockycode.engine.loop import Engine
        from rockycode.engine.trajectory import TrajectoryLogger
        from rockycode.tui.app import RockyCodeApp
        engine = Engine(model="fake", client=NS(), workdir=self.work,
                        registry=host_profile_registry(self.work, profile="read"),
                        trajectory_logger=TrajectoryLogger({}, enabled=False))
        RockyCodeApp(engine, permission="yolo", evolve="off")
        self.assertIsNone(engine.file_access)
        text, _ = await execute(engine.registry, "read_file", json.dumps({"path": str(self.out / "note.txt")}))
        self.assertIn("[blocked]", text)


if __name__ == "__main__":
    unittest.main()
