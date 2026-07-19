"""A tiny MCP stdio server for tests. Two tools, no external deps."""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@mcp.tool()
def shout(text: str) -> str:
    """Uppercase the text and add enthusiasm."""
    return text.upper() + "!"


if __name__ == "__main__":
    mcp.run()  # stdio transport
