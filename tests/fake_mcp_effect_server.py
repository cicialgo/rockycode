"""Observable file effects and explicit release/crash handshakes for MCP smokes."""
import asyncio
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("effect")


@mcp.tool()
def touch(path: str) -> str:
    """Create an empty file at path."""
    Path(path).write_text("")
    return "touched"


@mcp.tool()
async def hold(started: str, release: str, effect: str = "", crash: bool = False) -> str:
    """Signal entry, then wait for an explicit release before producing an effect."""
    Path(started).touch()
    while not Path(release).exists():
        await asyncio.sleep(0.005)
    if crash:
        os._exit(17)  # deliberate loss of the real stdio process
    if effect:
        Path(effect).touch()
    return "rested"


if __name__ == "__main__":
    mcp.run()  # stdio transport
