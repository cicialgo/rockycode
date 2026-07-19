"""bash tool: normal commands work, timeout kills the whole process group (no
orphaned children), and the timeout message is actionable. No network, ~5s."""
import asyncio
import tempfile
from pathlib import Path

import rockycode.engine.tools as T

T.BASH_TIMEOUT_S = 1  # force a short timeout for the test


async def main():
    wd = Path(tempfile.mkdtemp(prefix="rockybash-"))

    # normal command + exit code surfaced
    out = await T._bash("echo hi", workdir=wd)
    assert out.startswith("[exit 0]") and "hi" in out, out
    out = await T._bash("false", workdir=wd)
    assert out.startswith("[exit 1]"), out

    # timeout kills the GROUP: a backgrounded child would touch the marker at
    # +3s; the parent blocks at +5s. With a 1s timeout, killing only the shell
    # would let the child survive and create the marker.
    marker = wd / "child_marker"
    out = await T._bash(f"( sleep 3 && touch {marker} ) & sleep 5", workdir=wd)
    assert out.startswith("[timeout]"), out
    assert "ROCKYCODE_BASH_TIMEOUT" in out, "timeout message must say how to recover"
    await asyncio.sleep(4)  # past when the orphan would have created the marker
    assert not marker.exists(), "child survived — process group was not killed"

    print("SMOKE OK — bash timeout kills the group, no orphans. amaze!")


asyncio.run(main())
