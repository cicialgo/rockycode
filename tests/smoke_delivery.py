"""Deterministic FIFO, interruption and recovery contracts; no model calls."""
import asyncio
import unittest

from rockycode.engine.delivery import TurnDelivery


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.started = asyncio.Queue()
        self.release = asyncio.Event()
        self.cleanup = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleanup_release.set()
        self.order = []
        self.active = 0
        self.maximum = 0

        async def run(item):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.order.append(item.prompt)
            await self.started.put(item.prompt)
            try:
                if item.prompt == "first":
                    await self.release.wait()
                return item.prompt != "fail"
            except asyncio.CancelledError:
                self.cleanup.set()
                await self.cleanup_release.wait()
                raise
            finally:
                self.active -= 1

        self.delivery = TurnDelivery(run)

    async def asyncTearDown(self):
        self.cleanup_release.set()
        await self.delivery.close()

    async def first(self):
        await self.delivery.submit("first")
        self.assertEqual(await self.started.get(), "first")

    async def test_fresh_message_after_stop_runs(self):
        """A stop pauses the FIFO so queued text never resumes unattended, but a
        new message with nothing queued is the user acting now: it must start,
        not sit behind a Resume button (desktop e2e regression, 2026-10-15)."""
        await self.first()
        self.assertTrue(await self.delivery.stop())
        self.assertTrue(self.delivery.paused)
        receipt = await self.delivery.submit("third")
        self.assertEqual(receipt["status"], "started")
        self.assertEqual(await self.started.get(), "third")
        task = self.delivery.task
        if task is not None:  # a quick turn may already have drained and cleared it
            await task
        self.assertEqual(self.order, ["first", "third"])

    async def test_fifo_and_images_are_preserved(self):
        await self.first()
        images = ["one.png"]
        receipt = await self.delivery.submit("second", images=images)
        images.append("not-submitted.png")
        await self.delivery.submit("third")
        self.assertEqual(receipt["status"], "queued")
        self.assertEqual(self.delivery.pending[0].images, ["one.png"])
        self.assertEqual(self.order, ["first"])
        task = self.delivery.task
        self.release.set()
        await task
        self.assertEqual(self.order, ["first", "second", "third"])
        self.assertEqual(self.maximum, 1)

    async def test_interrupt_waits_for_cleanup_and_keeps_fifo(self):
        await self.first()
        await self.delivery.submit("queued")
        self.cleanup_release.clear()
        replacing = asyncio.create_task(self.delivery.submit("urgent", mode="interrupt"))
        await self.cleanup.wait()
        self.assertEqual(self.order, ["first"])
        self.cleanup_release.set()
        await replacing
        task = self.delivery.task
        if task:
            await task
        self.assertEqual(self.order, ["first", "urgent", "queued"])
        self.assertEqual(self.maximum, 1)

    async def test_stop_pauses_until_explicit_resume(self):
        await self.first()
        await self.delivery.submit("second")
        await self.delivery.stop()
        self.assertTrue(self.delivery.paused)
        self.assertEqual(self.order, ["first"])
        await self.delivery.resume()
        await self.delivery.task
        self.assertEqual(self.order, ["first", "second"])

    async def test_failure_pauses_remaining_messages(self):
        await self.first()
        await self.delivery.submit("fail")
        await self.delivery.submit("later")
        task = self.delivery.task
        self.release.set()
        await task
        self.assertTrue(self.delivery.paused)
        self.assertEqual([x.prompt for x in self.delivery.pending], ["later"])
        await self.delivery.resume()
        await self.delivery.task
        self.assertEqual(self.order, ["first", "fail", "later"])

    async def test_restored_messages_never_autostart(self):
        await self.first()
        receipt = await self.delivery.submit("saved")
        snapshot = self.delivery.snapshot()
        restored = TurnDelivery(self.delivery.run, pending=snapshot["pending"])
        try:
            self.assertTrue(restored.paused)
            self.assertFalse(restored.running)
            self.assertEqual(restored.pending[0].id, receipt["message_id"])
            self.assertTrue(await restored.remove(receipt["message_id"]))
            self.assertFalse(restored.pending)
        finally:
            await restored.close()

    async def test_invalid_input_cannot_cancel_current_turn(self):
        await self.first()
        for prompt, mode in [("new", "bad"), ("", "interrupt"), ("x" * 100001, "interrupt")]:
            with self.assertRaises(ValueError):
                await self.delivery.submit(prompt, mode=mode)
        self.assertTrue(self.delivery.running)
        self.assertFalse(self.cleanup.is_set())

    async def test_capacity_and_legacy_rejection(self):
        await self.first()
        self.delivery.max_pending = 1
        await self.delivery.submit("second")
        with self.assertRaises(ValueError):
            await self.delivery.submit("third")
        with self.assertRaises(ValueError):
            await self.delivery.submit("overlap", mode="reject")
        self.assertTrue(self.delivery.running)

    async def test_immediate_stop_does_not_run_pending_or_replay(self):
        await self.delivery.submit("first")
        await self.delivery.stop()
        self.assertFalse(self.order)
        self.assertEqual([x.prompt for x in self.delivery.pending], ["first"])
        await self.delivery.clear()
        await self.delivery.close()
        with self.assertRaises(ValueError):
            await self.delivery.submit("after-close")


if __name__ == "__main__":
    unittest.main()
