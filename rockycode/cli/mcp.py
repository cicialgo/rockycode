"""`rockycode mcp` — the Model Context Protocol server."""
from __future__ import annotations


import typer

from rockycode.banner import fail

# Credentials and endpoint come ONLY from ~/.rockycode (.env / keychain) or an
# explicit shell export. Project .env files are deliberately never loaded — a
# repo must not be able to supply a key or redirect where the key is sent
# (project_env_warnings tells the user when one tries).
from rockycode.cli._common import (  # noqa: F401
    console, REPO_ROOT, _config_model, _docker_preflight, _env_bool, _env_int, _load_prompt, _load_task_ids,
)


def mcp(
    transport: str = typer.Option("stdio", help="stdio or streamable-http (authenticated loopback only)."),
    port: int = typer.Option(8765, min=1, max=65535, help="Loopback HTTP port."),
) -> None:
    """Start a Model Context Protocol server on stdio (for Codex and other MCP clients).

    Not `rockycode serve` — serve speaks rocky's own JSON-RPC for editor
    clients. This command speaks MCP. stdout is the protocol only; logs go
    to stderr. Discover features with rocky_capabilities; exec defaults to read.
    HTTP requires ROCKYCODE_MCP_TOKEN and binds only 127.0.0.1. See docs/mcp-server.md.
    """
    from rockycode.mcp_server import serve_stdio, serve_http

    if transport == "stdio":
        serve_stdio()
    elif transport == "streamable-http":
        try:
            serve_http(port)
        except ValueError as exc:
            fail(console, str(exc))
            raise typer.Exit(1)
    else:
        fail(console, "transport must be stdio or streamable-http")
        raise typer.Exit(1)
