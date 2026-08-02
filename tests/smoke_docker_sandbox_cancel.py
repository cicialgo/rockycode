"""Real Docker regression: timeout/cancel must kill the container command tree.

Skipped by the default CORE gate; run via ``python tests/run_all.py --all``.
The delayed writes are load-bearing: killing only the host-side ``docker exec``
client leaves them behind and fails this test.
"""
import asyncio
import tempfile
from pathlib import Path

from rockycode.engine.sandbox import ChatSandbox


async def wait_for(path: Path, timeout: float = 3.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if path.exists():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"sandbox command never reached ready marker: {path.name}")


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="rocky-sandbox-cancel-") as tmpdir:
        workdir = Path(tmpdir)
        sandbox = await ChatSandbox.start(workdir)
        try:
            # Explicit cancellation: command reaches the sleep, then must die
            # before its delayed write touches the host-mounted workspace.
            task = asyncio.create_task(sandbox.exec(
                "printf ready > cancel-ready; sleep 1; printf leaked > cancel-leaked"
            ))
            await wait_for(workdir / "cancel-ready")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("CancelledError was not propagated")
            await asyncio.sleep(1.1)
            assert not (workdir / "cancel-leaked").exists(), \
                "cancelled container command kept running against /workspace"

            # Timeout follows the same cleanup path and must also kill descendants.
            out, code = await sandbox.exec(
                "printf ready > timeout-ready; sleep 1; printf leaked > timeout-leaked",
                timeout=0.2,
            )
            assert code == 124 and out.startswith("[timeout]"), (out, code)
            await asyncio.sleep(1.1)
            assert not (workdir / "timeout-leaked").exists(), \
                "timed-out container command kept running against /workspace"

            # Cleanup is per-exec: the long-lived sandbox remains usable.
            out, code = await sandbox.exec("printf ok > still-usable")
            assert code == 0 and (workdir / "still-usable").read_text() == "ok", out
        finally:
            await sandbox.stop()

    print("DOCKER SANDBOX CANCEL SMOKE OK — no orphaned workspace writes")


asyncio.run(main())
