"""MCP admission and shutdown against a real FastMCP stdio process.

File handshakes establish dispatch/queue order; timeouts only bound failures.
Caller cancellation after dispatch still permits effects: this is NOT rollback.
No network, API key, or writes outside TemporaryDirectory.
"""
import asyncio
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

from rockycode.engine import mcp as module
from rockycode.engine.mcp import MCPManager, ServerConfig, _result_text

SERVER = Path(__file__).resolve().parent / "fake_mcp_effect_server.py"


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


async def cancelled(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
    raise AssertionError("caller must receive cancellation")


async def rejected(task):
    try:
        await asyncio.wait_for(asyncio.shield(task), 2)
    except RuntimeError as exc:
        assert "not running" in str(exc), exc
    else:
        raise AssertionError("stopping/stopped actor must reject the call")


async def scenario(root, *, crash=False, forced=False):
    cfg = {"effect": ServerConfig("effect", sys.executable, [str(SERVER)])}
    mgr = MCPManager(cfg)
    tasks = []
    releases = []

    def spawn(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    async def hold(label, **extra):
        started, release = root / (label + ".started"), root / (label + ".release")
        releases.append(release)
        task = spawn(actor.call("hold", {"started": str(started), "release": str(release), **extra}))
        await until(started.exists)
        return task, release

    try:
        await asyncio.wait_for(mgr.start(), 15)
        assert not mgr.failures, mgr.failures
        actor = mgr.actors["effect"]
        if forced:
            active, _ = await hold("forced", effect=str(root / "forced.effect"))
            queued = spawn(actor.call("touch", {"path": str(root / "after-forced")}))
            await until(lambda: actor._requests.qsize() == 1)
            with patch.object(module, "STOP_TIMEOUT_S", 0.05):
                await asyncio.wait_for(actor.stop(), 5)
            await rejected(active)
            await rejected(queued)
            assert actor._task.done() and actor._requests.empty()
            assert not (root / "after-forced").exists()
            return
        if crash:
            active, release = await hold("crash", crash=True)
            queued = spawn(actor.call("touch", {"path": str(root / "after-crash")}))
            await until(lambda: actor._requests.qsize() == 1)
            release.touch()
            results = await asyncio.wait_for(
                asyncio.shield(asyncio.gather(active, queued, return_exceptions=True)), 3
            )
            assert all(isinstance(result, Exception) for result in results), results
            await asyncio.wait_for(asyncio.shield(actor._task), 3)
            await rejected(spawn(actor.call("touch", {"path": str(root / "late-crash")})))
            assert actor._requests.empty()
            assert not (root / "after-crash").exists()
            assert not (root / "late-crash").exists()
            return

        # The server is verifiably busy before the doomed call enters its queue.
        busy, release = await hold("busy")
        doomed = spawn(actor.call("touch", {"path": str(root / "cancelled-queued")}))
        await until(lambda: actor._requests.qsize() == 1)
        await cancelled(doomed)
        release.touch()
        result = await busy
        assert not result.isError and _result_text(result) == "rested", result
        barrier = await actor.call("touch", {"path": str(root / "barrier")})
        assert not barrier.isError and _result_text(barrier) == "touched", barrier
        assert (root / "barrier").exists()
        assert not (root / "cancelled-queued").exists()

        # Preserve the established in-flight semantics with an observable effect.
        running, release = await hold("running", effect=str(root / "running.effect"))
        await cancelled(running)
        release.touch()
        await actor.call("touch", {"path": str(root / "running-barrier")})
        assert (root / "running.effect").exists(), "in-flight cancellation is not rollback"

        busy, release = await hold("stopping")
        pending = spawn(actor.call("touch", {"path": str(root / "before-stop")}))
        await until(lambda: actor._requests.qsize() == 1)
        stopping = spawn(mgr.stop())
        await until(lambda: actor._requests.qsize() == 2)
        await rejected(spawn(actor.call("touch", {"path": str(root / "during-stop")})))
        release.touch()
        assert _result_text(await busy) == "rested"
        assert _result_text(await pending) == "touched"
        await stopping
        assert (root / "before-stop").exists()
        assert not (root / "during-stop").exists()
        await rejected(spawn(actor.call("touch", {"path": str(root / "after-stop")})))
        assert not (root / "after-stop").exists()
        await actor.stop()  # idempotent: no abandoned sentinels
        assert actor._requests.empty()
    finally:
        for release in releases:
            release.touch()
        with suppress(Exception):
            await mgr.stop()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def main():
    with tempfile.TemporaryDirectory(prefix="rockymcp-admission-") as directory:
        root = Path(directory)
        for name in ("admission", "crash", "forced"):
            workdir = root / name
            workdir.mkdir()
            await scenario(workdir, crash=name == "crash", forced=name == "forced")
    print("PASS: real stdio admission, running cancellation, graceful/forced stop, crash, and queue drain")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), 45))
