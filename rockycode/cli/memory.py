"""`rockycode memory …` — list / show / rm / search / reindex / edit."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

import typer

from rockycode.banner import confused, fail, info

# Credentials and endpoint come ONLY from ~/.rockycode (.env / keychain) or an
# explicit shell export. Project .env files are deliberately never loaded — a
# repo must not be able to supply a key or redirect where the key is sent
# (project_env_warnings tells the user when one tries).
from rockycode.cli._common import (  # noqa: F401
    console, REPO_ROOT, _config_model, _docker_preflight, _env_bool, _env_int, _load_prompt, _load_task_ids,
)


memory_app = typer.Typer(
    help="Inspect rocky's memory. Files under .rockycode/memory are the truth — edit them freely.",
    no_args_is_help=True,
)


def _store(workdir: Optional[Path]) -> "MemoryStore":  # noqa: F821 — lazy import below
    from rockycode.memory import MemoryStore
    return MemoryStore.for_workdir((workdir or Path.cwd()).resolve())


_WORKDIR_OPT = typer.Option(None, "--workdir", "-C", help="Project directory. Defaults to cwd.")


@memory_app.command("list")
def memory_list(workdir: Optional[Path] = _WORKDIR_OPT, all: bool = typer.Option(False, "--all", "-a", help="Include archived.")) -> None:
    """List memories (name, type, description)."""
    memories = _store(workdir).load_all(include_archived=all)
    if not memories:
        info(console, "no memories yet. rocky remembers via the `remember` tool or /remember in chat.")
        return
    for m in memories:
        mark = "[dim]archived · [/dim]" if m.status == "archived" else ""
        console.print(f"  [bold]{m.name}[/bold] [dim]({mark}{m.type})[/dim] — {m.description}")


@memory_app.command("show")
def memory_show(name: str, workdir: Optional[Path] = _WORKDIR_OPT) -> None:
    """Print one memory in full (frontmatter + body)."""
    mem = _store(workdir).get(name)
    if mem is None or mem.path is None:
        fail(console, f"no memory named '{name}'.")
        raise typer.Exit(1)
    console.print(f"[dim]{mem.path}[/dim]\n{mem.path.read_text()}")


@memory_app.command("rm")
def memory_rm(name: str, workdir: Optional[Path] = _WORKDIR_OPT) -> None:
    """Archive a memory (moved to archive/, never deleted)."""
    if _store(workdir).archive(name):
        info(console, f"archived '{name}' → .rockycode/memory/archive/")
    else:
        fail(console, f"no active memory named '{name}'.")
        raise typer.Exit(1)


@memory_app.command("search")
def memory_search(
    query: str,
    workdir: Optional[Path] = _WORKDIR_OPT,
    keyword: bool = typer.Option(False, "--keyword", help="Skip embeddings; plain substring search."),
) -> None:
    """Semantic search (Ollama embeddings + keyword hybrid); --keyword for substring only."""
    store = _store(workdir)
    hits = []
    if not keyword:
        from rockycode.memory.index import IndexUnavailable, MemoryIndex, search_sync
        try:
            hits = [m for m, _ in search_sync(MemoryIndex(store), query)]
        except IndexUnavailable as e:
            confused(console, f"semantic index unavailable ({e}); falling back to substring.")
    if not hits:
        hits = store.search(query)
    if not hits:
        confused(console, f"nothing matches '{query}'.")
        return
    for m in hits:
        console.print(f"  [bold]{m.name}[/bold] [dim]({m.type})[/dim] — {m.description}")


@memory_app.command("reindex")
def memory_reindex(
    workdir: Optional[Path] = _WORKDIR_OPT,
    force: bool = typer.Option(False, "--force", help="Re-embed everything, ignoring hashes."),
) -> None:
    """Rebuild index.db from the markdown files (it is always safe to delete)."""
    from rockycode.memory.index import IndexUnavailable, MemoryIndex, reindex_sync
    try:
        indexed, kept, removed = reindex_sync(MemoryIndex(_store(workdir)), force=force)
    except IndexUnavailable as e:
        fail(console, f"semantic index unavailable: {e}")
        raise typer.Exit(1)
    except Exception as e:  # noqa: BLE001 — usually Ollama not running
        fail(console, f"reindex failed ({type(e).__name__}: {e}). is ollama running?")
        raise typer.Exit(1)
    info(console, f"indexed {indexed}, unchanged {kept}, removed {removed}")


@memory_app.command("edit")
def memory_edit(name: str, workdir: Optional[Path] = _WORKDIR_OPT) -> None:
    """Open a memory file in $EDITOR."""
    mem = _store(workdir).get(name)
    if mem is None or mem.path is None:
        fail(console, f"no memory named '{name}'.")
        raise typer.Exit(1)
    editor = os.getenv("EDITOR", "vi")
    subprocess.run([editor, str(mem.path)])
