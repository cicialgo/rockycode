"""Host integration with controlled streams, cancellation and durable text."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rockycode_desktop.host import Host


class ControlledHost(Host):
    def __init__(self, directory):
        super().__init__(directory, fixture=True)
        self.started = asyncio.Queue()
        self.release = asyncio.Event()
        self.cleanup = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleanup_release.set()
        self.order = []
        self.active = self.maximum = 0

    async def fixture_stream(self, thread, prompt):
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.order.append(prompt)
        await self.started.put(prompt)
        try:
            if prompt == "first":
                await self.release.wait()
            if prompt == "fail":
                raise RuntimeError("controlled failure")
            yield "text_delta", {"text": prompt + " completed"}
        except asyncio.CancelledError:
            self.cleanup.set()
            await self.cleanup_release.wait()
            raise
        finally:
            self.active -= 1


class DeliveryHostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.host = ControlledHost(self.temp.name)
        self.thread = await self.host.dispatch("thread/create", {"workspace": self.temp.name})
        self.args = {"threadId": self.thread["id"]}

    async def asyncTearDown(self):
        self.host.cleanup_release.set()
        await self.host.close()
        self.temp.cleanup()

    async def send(self, prompt, delivery="queue"):
        return await self.host.dispatch("turn/start", {**self.args, "prompt": prompt, "delivery": delivery})

    async def first(self):
        await self.send("first")
        self.assertEqual(await self.host.started.get(), "first")

    async def test_queue_does_not_touch_running_history_and_preserves_draft(self):
        await self.first()
        await self.send("second")
        await self.host.dispatch("draft/save", {**self.args, "content": "unsent next idea"})
        await self.send("third")
        self.assertEqual(len(self.thread["messages"]), 2)
        self.assertEqual(len(self.thread["delivery"]["pending"]), 2)
        task = self.host.delivery(self.thread).task
        self.host.release.set()
        await task
        self.assertEqual(self.host.order, ["first", "second", "third"])
        self.assertEqual(self.thread["draft"], "unsent next idea")
        self.assertEqual(self.host.maximum, 1)

    async def test_interrupt_awaits_cleanup_and_retains_pending(self):
        await self.first()
        await self.send("queued")
        self.host.cleanup_release.clear()
        replacing = asyncio.create_task(self.send("urgent", "interrupt"))
        await self.host.cleanup.wait()
        self.assertEqual(self.host.order, ["first"])
        self.host.cleanup_release.set()
        await replacing
        task = self.host.delivery(self.thread).task
        if task:
            await task
        self.assertEqual(self.host.order, ["first", "urgent", "queued"])
        self.assertEqual(self.thread["messages"][1]["status"], "cancelled")
        self.assertEqual(self.host.maximum, 1)

    async def test_stop_restore_and_resume_are_explicit(self):
        await self.first()
        await self.send("saved")
        await self.host.dispatch("turn/cancel", self.args)
        self.assertTrue(self.thread["delivery"]["paused"])
        reopened = ControlledHost(self.temp.name)
        try:
            saved = reopened.threads[self.thread["id"]]
            self.assertEqual(saved["delivery"]["pending"][0]["prompt"], "saved")
            self.assertTrue(saved["delivery"]["paused"])
            self.assertFalse(reopened.tasks)
            await reopened.dispatch("turn/resume", self.args)
            task = reopened.delivery(saved).task
            if task:
                await task
            self.assertEqual(reopened.order, ["saved"])
        finally:
            await reopened.close()

    async def test_failure_pauses_and_message_can_be_withdrawn(self):
        await self.first()
        await self.send("fail")
        receipt = await self.send("later")
        task = self.host.delivery(self.thread).task
        self.host.release.set()
        await task
        self.assertTrue(self.thread["delivery"]["paused"])
        self.assertEqual(self.thread["messages"][-1]["status"], "error")
        response = await self.host.dispatch("turn/dequeue", {**self.args, "messageId": receipt["message_id"]})
        self.assertTrue(response["removed"])
        self.assertFalse(self.thread["delivery"]["pending"])

    async def test_restart_retains_claimed_text_before_transcript_checkpoint(self):
        item = {"id": "claimed", "prompt": "not yet checkpointed", "images": [], "mode": "queue"}
        self.thread["delivery"] = {"pending": [], "paused": False, "active": item}
        self.host.save(self.thread)
        reopened = ControlledHost(self.temp.name)
        try:
            saved = reopened.threads[self.thread["id"]]
            self.assertEqual(saved["delivery"]["pending"], [item])
            self.assertTrue(saved["delivery"]["paused"])
            self.assertFalse(reopened.tasks)
        finally:
            await reopened.close()

    async def test_workspace_is_checked_when_queued_message_starts(self):
        with tempfile.TemporaryDirectory() as workspace:
            self.thread["workspace"] = workspace
            await self.first()
            await self.send("later")
        task = self.host.delivery(self.thread).task
        self.host.release.set()
        await task
        self.assertEqual(self.host.order, ["first"])
        self.assertEqual(self.thread["messages"][-1]["status"], "error")
        self.assertTrue(self.thread["delivery"]["paused"])

    async def test_invalid_delivery_cannot_interrupt(self):
        await self.first()
        with self.assertRaises(ValueError):
            await self.send("invalid", "bad")
        self.assertFalse(self.host.cleanup.is_set())


if __name__ == "__main__":
    unittest.main()
