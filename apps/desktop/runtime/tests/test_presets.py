import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rockycode_desktop.host import Host
from rockycode_desktop.presets import BODY_LIMIT, MAX_PRESETS, PresetLibrary


def _mode(path):
    return path.stat().st_mode & 0o777


class PresetLibraryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.library = PresetLibrary(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def test_create_rename_and_delete_round_trip(self):
        self.assertEqual(self.library.list(), [])
        self.assertFalse((self.root / "prompt-presets.json").exists())
        created = self.library.save({"name": "  审阅\n笔记  ", "body": "  请逐段核对来源。  "})
        self.assertEqual(created["name"], "审阅 笔记")
        self.assertEqual(created["body"], "  请逐段核对来源。  ")
        self.assertEqual(len(created["id"]), 32)
        renamed = self.library.save({"id": created["id"], "name": "审阅", "body": "只看证据。"})
        self.assertEqual(renamed["id"], created["id"])
        self.assertEqual(renamed["body"], "只看证据。")
        self.assertGreaterEqual(renamed["updatedAt"], created["updatedAt"])
        listed = self.library.list()
        self.assertEqual([item["name"] for item in listed], ["审阅"])
        self.library.delete(created["id"])
        self.assertEqual(self.library.list(), [])

    def test_file_permissions_and_public_fields_only(self):
        self.library.save({"name": "Review", "body": "Read the sources."})
        path = self.root / "prompt-presets.json"
        self.assertEqual(_mode(path), 0o600)
        self.assertEqual(_mode(self.root), 0o700)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(saved), {"version", "presets"})
        self.assertEqual(set(saved["presets"][0]), {"id", "name", "body", "updatedAt"})
        self.assertNotIn("apiKey", path.read_text(encoding="utf-8"))

    def test_rejects_credentials_models_and_permission_fields(self):
        with self.assertRaisesRegex(ValueError, "name and prompt text only"):
            self.library.save({"name": "Review", "body": "Read the sources.", "apiKey": "sk-secret", "model": "demo", "permissionMode": "yolo"})
        self.assertFalse((self.root / "prompt-presets.json").exists())
        self.assertEqual(self.library.list(), [])

    def test_duplicate_names_are_case_insensitive(self):
        self.library.save({"name": "Review", "body": "First"})
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.library.save({"name": " review ", "body": "Second"})
        self.assertEqual(len(self.library.list()), 1)

    def test_invalid_name_and_body(self):
        for name in ("", "   ", "x" * 81):
            with self.assertRaisesRegex(ValueError, "Invalid preset name"):
                self.library.save({"name": name, "body": "ok"})
        for body in ("", " \n\t ", "y" * (BODY_LIMIT + 1)):
            with self.assertRaisesRegex(ValueError, "empty or too long"):
                self.library.save({"name": "ok", "body": body})
        self.assertEqual(self.library.list(), [])

    def test_limit_counts_creates_not_edits(self):
        first = None
        for index in range(MAX_PRESETS):
            saved = self.library.save({"name": f"preset-{index}", "body": "body"})
            first = first or saved
        with self.assertRaisesRegex(ValueError, "list is full"):
            self.library.save({"name": "one-more", "body": "body"})
        edited = self.library.save({"id": first["id"], "name": "preset-0", "body": "updated"})
        self.assertEqual(edited["body"], "updated")
        self.assertEqual(len(self.library.list()), MAX_PRESETS)
        self.library.delete(first["id"])
        self.library.save({"name": "one-more", "body": "body"})
        self.assertEqual(len(self.library.list()), MAX_PRESETS)

    def test_corrupt_file_can_be_replaced_but_a_newer_version_cannot(self):
        path = self.root / "prompt-presets.json"
        path.write_text("{", encoding="utf-8")
        reloaded = PresetLibrary(self.root)
        self.assertEqual(reloaded.list(), [])
        reloaded.save({"name": "Recovered", "body": "Still here."})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 1)
        path.write_text(json.dumps({"version": 2, "presets": [{"id": "a" * 32, "name": "Keep", "body": "secret"}]}), encoding="utf-8")
        protected = PresetLibrary(self.root)
        self.assertEqual(protected.list(), [])
        with self.assertRaisesRegex(ValueError, "cannot be updated"):
            protected.save({"name": "Other", "body": "nope"})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 2)

    def test_drops_unknown_fields_and_invalid_entries_on_load(self):
        kept = {"id": "a" * 32, "name": "Kept", "body": "visible", "updatedAt": 5, "apiKey": "sk-nope", "model": "demo"}
        path = self.root / "prompt-presets.json"
        path.write_text(json.dumps({"version": 1, "presets": [kept, {"id": "bad", "name": "No", "body": "x"}, kept]}), encoding="utf-8")
        loaded = PresetLibrary(self.root).list()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(set(loaded[0]), {"id", "name", "body", "updatedAt"})
        self.assertNotIn("apiKey", loaded[0])


class PresetHostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.host = Host(self.directory.name, fixture=True)
        self.thread = await self.host.dispatch("thread/create", {"workspace": str(self.workspace)})

    async def asyncTearDown(self):
        await self.host.close()
        self.directory.cleanup()

    async def test_presets_survive_restart_and_stay_out_of_the_project(self):
        await self.host.dispatch("draft/save", {"threadId": self.thread["id"], "content": "尚未发送"})
        saved = await self.host.dispatch("preset/save", {"name": "提纲", "body": "先列证据，再写结论。"})
        self.assertEqual(self.thread["draft"], "尚未发送")
        self.assertEqual(self.thread["status"], "idle")
        snapshot = await self.host.dispatch("initialize", {})
        self.assertEqual(snapshot["presets"][0]["name"], "提纲")
        self.assertNotIn("presets", self.thread)
        database = sqlite3.connect(self.root / "workspace.sqlite3")
        try:
            blob = " ".join(row[0] for row in database.execute("SELECT data FROM threads"))
        finally:
            database.close()
        self.assertNotIn("先列证据，再写结论。", blob)
        self.assertTrue((self.root / "prompt-presets.json").is_file())
        self.assertFalse((self.workspace / "prompt-presets.json").exists())
        reopened = Host(self.directory.name, fixture=True)
        try:
            listed = await reopened.dispatch("preset/list", {})
            self.assertEqual(listed["presets"][0]["id"], saved["preset"]["id"])
            self.assertEqual(reopened.threads[self.thread["id"]]["draft"], "尚未发送")
            other = self.workspace / "other"
            other.mkdir()
            second = await reopened.dispatch("thread/create", {"workspace": str(other)})
            again = await reopened.dispatch("preset/list", {})
            self.assertEqual(again["presets"][0]["name"], "提纲")
            self.assertNotIn("presets", second)
        finally:
            await reopened.close()

    async def test_unknown_preset_method_and_missing_id(self):
        with self.assertRaisesRegex(ValueError, "Unknown method"):
            await self.host.dispatch("preset/nope", {})
        with self.assertRaisesRegex(ValueError, "Preset not found"):
            await self.host.dispatch("preset/delete", {"id": "b" * 32})


if __name__ == "__main__":
    unittest.main()
