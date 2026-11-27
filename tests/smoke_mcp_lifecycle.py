"""Deterministic actor scheduling/teardown races complement real stdio tests."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import patch

from rockycode.engine import mcp as module
from rockycode.engine.mcp import ServerConfig, _ServerActor


class Session:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.closing = asyncio.Event()
        self.exit_allowed = asyncio.Event()
        self.exit_allowed.set()
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closing.set()
        await self.exit_allowed.wait()

    async def initialize(self):
        pass

    async def list_tools(self):
        return SimpleNamespace(tools=[])

    async def call_tool(self, name, args):
        self.calls.append(name)
        if name == "hold":
            self.entered.set()
            await self.release.wait()
        elif name == "error":
            raise ValueError("per-call error")
        return name


@asynccontextmanager
async def stdio(*args, **kwargs):
    yield None, None


class Lifecycle(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = Session()
        self.tasks = []
        self.patches = [patch("mcp.ClientSession", return_value=self.session),
                        patch("mcp.client.stdio.stdio_client", stdio)]
        for p in self.patches:
            p.start()
        self.actor = _ServerActor(ServerConfig("fixture", "unused"))
        await asyncio.wait_for(self.actor.ready, 1)

    async def asyncTearDown(self):
        self.session.release.set()
        self.session.exit_allowed.set()
        if not self.actor._task.done():
            self.actor._task.cancel()
        await asyncio.gather(self.actor._task, return_exceptions=True)
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for p in reversed(self.patches):
            p.stop()

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    async def rejected(self, awaitable):
        with self.assertRaisesRegex(RuntimeError, "not running"):
            await asyncio.wait_for(asyncio.shield(awaitable), 0.3)

    async def busy(self):
        task = self.spawn(self.actor.call("hold", {}))
        await asyncio.wait_for(self.session.entered.wait(), 1)
        return task

    async def queued(self, name="queued"):
        task = self.spawn(self.actor.call(name, {}))
        async with asyncio.timeout(1):
            while self.actor._requests.qsize() == 0:
                await asyncio.sleep(0)
        return task

    async def test_actor_cancellation_settles_active_and_queue(self):
        active = await self.busy()
        queued = await self.queued()
        self.actor._task.cancel()
        await asyncio.gather(self.actor._task, return_exceptions=True)
        await self.rejected(active)
        await self.rejected(queued)
        self.assertTrue(self.actor._requests.empty())

    async def test_stop_timeout_settles_active_and_queue(self):
        active = await self.busy()
        queued = await self.queued()
        with patch.object(module, "STOP_TIMEOUT_S", 0.02, create=True):
            await asyncio.wait_for(self.actor.stop(), 1)
        await self.rejected(active)
        await self.rejected(queued)
        self.assertTrue(self.actor._requests.empty())

    async def test_cancelled_stopper_does_not_cancel_actor(self):
        active = await self.busy()
        first = self.spawn(self.actor.stop())
        second = self.spawn(self.actor.stop())
        await asyncio.sleep(0)  # both stoppers reach their first await
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(self.actor._task.cancelling())
        self.session.release.set()
        self.assertEqual(await asyncio.wait_for(active, 1), "hold")
        await asyncio.wait_for(second, 1)
        self.assertTrue(self.actor._requests.empty())

    async def test_rejects_calls_during_slow_teardown(self):
        self.session.exit_allowed.clear()
        stopping = self.spawn(self.actor.stop())
        await asyncio.wait_for(self.session.closing.wait(), 1)
        await self.rejected(self.spawn(self.actor.call("late", {})))
        self.session.exit_allowed.set()
        await stopping
        self.assertEqual(self.session.calls, [])

    async def test_cancelled_only_stopper_keeps_shutdown_deadline(self):
        active = await self.busy()
        with patch.object(module, "STOP_TIMEOUT_S", 0.02, create=True):
            stopping = self.spawn(self.actor.stop())
            await asyncio.sleep(0)
            stopping.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await stopping
            await self.rejected(active)
            await asyncio.wait_for(self.actor.stop(), 1)
        self.assertTrue(self.actor._task.done())
        self.assertTrue(self.actor._requests.empty())

    async def test_cancel_between_dequeue_and_sdk_dispatch(self):
        class CancelOnGet(asyncio.Queue):
            async def get(self):
                req = await super().get()
                if req is not None and req[0] == "doomed":
                    asyncio.get_running_loop().call_soon(req[2].cancel)
                return req

        original_wait_for = asyncio.wait_for

        async def scheduled_wait_for(awaitable, timeout):
            # Model the supported Python 3.11 scheduler even when running 3.12+.
            return await original_wait_for(asyncio.ensure_future(awaitable), timeout)

        with patch.object(module.asyncio, "wait_for", scheduled_wait_for):
            active = await self.busy()
            self.actor._requests = CancelOnGet()
            doomed = await self.queued("doomed")
            self.session.release.set()
            await active
            with self.assertRaises(asyncio.CancelledError):
                await doomed
            self.assertEqual(await self.actor.call("barrier", {}), "barrier")
            self.assertNotIn("doomed", self.session.calls)

    async def test_completed_entries_and_sentinels_are_drained(self):
        active = await self.busy()
        cancelled = await self.queued("cancelled")
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        completed = asyncio.get_running_loop().create_future()
        completed.set_result("already settled")
        self.actor._requests.put_nowait(("completed", {}, completed))
        stopping = self.spawn(self.actor.stop())
        await asyncio.sleep(0)
        late = asyncio.get_running_loop().create_future()
        self.actor._requests.put_nowait(("late", {}, late))
        self.actor._requests.put_nowait(None)
        self.session.release.set()
        await active
        await stopping
        await self.rejected(late)
        await self.actor.stop()
        self.assertTrue(self.actor._requests.empty())
        self.assertEqual(self.session.calls, ["hold"])

    async def test_per_call_error_and_timeout_leave_actor_usable(self):
        # Don't use assertRaises here: it clears traceback frames belonging to
        # the still-serving actor coroutine when consuming a cross-task error.
        try:
            await self.actor.call("error", {})
        except ValueError as exc:
            self.assertEqual(str(exc), "per-call error")
        else:
            self.fail("per-call error was not surfaced")
        with patch.object(module, "CALL_TIMEOUT_S", 0.02):
            try:
                await self.actor.call("hold", {})
            except TimeoutError:
                pass
            else:
                self.fail("per-call deadline was not enforced")
        self.assertEqual(await self.actor.call("next", {}), "next")

    async def test_cancel_before_actor_coroutine_starts(self):
        actor = _ServerActor(ServerConfig("early", "unused"))
        caller = self.spawn(actor.call("queued", {}))
        actor._task.cancel()
        await asyncio.gather(actor._task, return_exceptions=True)
        await self.rejected(actor.ready)
        await self.rejected(caller)
        await asyncio.wait_for(actor.stop(), 1)
        self.assertTrue(actor._requests.empty())

    async def test_setup_failure_settles_ready_and_queue(self):
        with patch("mcp.StdioServerParameters", side_effect=ValueError("setup failed")):
            actor = _ServerActor(ServerConfig("broken", "unused"))
            with self.assertRaisesRegex(ValueError, "setup failed"):
                await asyncio.wait_for(asyncio.shield(actor.ready), 0.3)
            await self.rejected(self.spawn(actor.call("late", {})))
            await actor.stop()
            self.assertTrue(actor._requests.empty())


if __name__ == "__main__":
    main()
