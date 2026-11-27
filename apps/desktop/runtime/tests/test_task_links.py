"""Desktop task entry points and real Engine tool round trips, all offline."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rockycode_desktop.adapter import EngineAdapter
from rockycode_desktop.host import Host
from rockycode_desktop.messages import HostError
from test_shared_runtime import fake_client


class TaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"ROCKYCODE_HOME": str(self.root / "home"), "ROCKYCODE_MODEL": "fake"})
        self.env.start()
        self.host = Host(self.root / "data", fixture=True)
        self.a = await self.host.dispatch("thread/create", {"workspace": str(self.root)})

    async def asyncTearDown(self):
        await self.host.close()
        self.env.stop()
        self.temp.cleanup()

    async def child(self):
        return await self.host.dispatch("task/create", {"threadId": self.a["id"], "prompt": "check the implementation"})

    async def test_new_task_is_independent_and_durable(self):
        self.a.update(draft="parent draft", note="user note", permissionMode="yolo", model="chosen")
        self.host.save(self.a, [{"role": "user", "content": "private history"}])
        b = await self.child()
        self.assertEqual(b["parentTaskId"], self.a["id"])
        self.assertEqual((b["draft"], b["model"], b["permissionMode"]), ("check the implementation", "chosen", "ask"))
        self.assertEqual((b["messages"], b["note"], self.host.store.history(b["id"])), ([], "", []))
        self.assertEqual(self.a["draft"], "parent draft")
        self.assertFalse(self.host.tasks)
        await self.host.dispatch("task/send", {"threadId": self.a["id"], "target": b["id"], "message": "question"})
        self.assertEqual(b["taskInbox"][0]["sender"], self.a["id"])
        self.assertFalse(self.host.tasks)
        self.host.save(b)  # link fields are derived on read, never persisted with the thread
        stored = json.loads(self.host.store.db.execute("SELECT data FROM threads WHERE id=?", (b["id"],)).fetchone()[0])
        self.assertNotIn("taskInbox", stored)
        self.assertNotIn("parentTaskId", stored)
        await self.host.close()
        self.host = Host(self.root / "data", fixture=True)
        restored = {t["id"]: t for t in self.host.snapshot()["threads"]}
        self.assertEqual(restored[b["id"]]["taskInbox"][0]["content"], "question")
        self.assertEqual(restored[b["id"]]["parentTaskId"], self.a["id"])
        self.assertEqual(restored[b["id"]]["draft"], "check the implementation")

    async def test_cross_workspace_and_deleted_target_refused(self):
        other = self.root / "other"; other.mkdir()
        b = await self.host.dispatch("thread/create", {"workspace": str(other)})
        with self.assertRaises(HostError) as refused:  # the store's refusal arrives catalog-keyed, never as English
            await self.host.dispatch("task/send", {"threadId": self.a["id"], "target": b["id"], "message": "no"})
        self.assertEqual(refused.exception.key, "diagnostic.taskWorkspace")
        await self.host.dispatch("thread/delete", {"threadId": b["id"]})
        with self.assertRaises(HostError) as refused:
            await self.host.dispatch("task/send", {"threadId": self.a["id"], "target": b["id"], "message": "no"})
        self.assertEqual(refused.exception.key, "diagnostic.sessionMissing")
        with self.assertRaises(HostError) as refused:
            await self.host.dispatch("task/create", {"threadId": self.a["id"], "prompt": " "})
        self.assertEqual((refused.exception.key, refused.exception.params), ("diagnostic.taskText", {"max": 20000}))
        with self.assertRaises(HostError) as refused:
            await self.host.dispatch("task/dismiss", {"threadId": self.a["id"], "messageId": 7})
        self.assertEqual(refused.exception.key, "diagnostic.taskMessageUnknown")
        self.assertEqual(self.host.links.list(self.a["id"])[0]["id"], self.a["id"])

    async def test_creation_and_mail_do_not_interrupt_running_source(self):
        release = asyncio.Event()

        async def stream(thread, prompt):
            await release.wait()
            yield "text_delta", {"text": "finished"}

        self.host.fixture_stream = stream
        await self.host.dispatch("turn/start", {"threadId": self.a["id"], "prompt": "ongoing"})
        running = self.host.delivery(self.a).task
        self.a.update(draft="keep this", note="also keep this", permissionMode="careful")
        b = await self.child()
        self.assertEqual(b["permissionMode"], "careful")
        await self.host.dispatch("task/send", {"threadId": b["id"], "target": self.a["id"], "message": "mail while busy"})
        self.assertIs(self.host.delivery(self.a).task, running)
        self.assertEqual(self.host.delivery(self.a).pending, [])
        self.assertEqual(self.a["draft"], "keep this")
        self.assertEqual(self.a["note"], "also keep this")
        release.set()
        await running
        self.assertEqual(self.a["messages"][-1]["status"], "complete")

    async def turn(self, thread, prompt, allowed=True):
        await self.host.dispatch("turn/start", {"threadId": thread["id"], "prompt": prompt})
        async with asyncio.timeout(10):
            while self.host.delivery(thread).running:
                if thread.get("approval"):
                    await self.host.dispatch("approval/respond", {"threadId": thread["id"],
                        "approvalId": thread["approval"]["id"], "decision": "once" if allowed else "deny"})
                await asyncio.sleep(.005)
        self.assertEqual(thread["messages"][-1]["status"], "complete", thread["messages"][-1])

    async def test_real_engines_exchange_messages_and_respect_denial(self):
        b = await self.child()
        self.host.fixture = False
        clients = iter([
            fake_client([("send_task_message", {"target": b["id"], "message": "question"}), "sent",
                         ("read_task_messages", {}), "received",
                         ("create_new_task", {"prompt": "denied child"}), "denied",
                         ("create_new_task", {"prompt": "approved child"}), "created"]),
            fake_client([("read_task_messages", {}),
                         ("send_task_message", {"target": self.a["id"], "message": "answer"}), "replied"]),
        ])
        def factory(*args):
            adapter = EngineAdapter(*args, client=next(clients), include_user=False, mcp=False,
                                    lsp=False, memory=False, web=False, skills=False)
            adapter.engine.hooks.clear()
            return adapter
        with patch("rockycode_desktop.host.EngineAdapter", factory):
            await self.turn(self.a, "send question")
            self.assertEqual(b["messages"], [])  # stored != started
            await self.turn(b, "read and reply")
            await self.turn(self.a, "read reply")
            tool_rows = [m for m in self.host.store.history(self.a["id"]) if m["role"] == "tool"]
            self.assertEqual(json.loads(tool_rows[-1]["content"])["messages"][0]["content"], "answer")
            count = len(self.host.threads)
            await self.turn(self.a, "create denied child", allowed=False)
            self.assertEqual(len(self.host.threads), count)
            await self.turn(self.a, "create approved child")
            self.assertEqual(len(self.host.threads), count + 1)
            created = next(t for t in self.host.threads.values() if t["draft"] == "approved child")
            self.assertEqual(created["messages"], [])


if __name__ == "__main__":
    unittest.main()
