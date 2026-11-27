"""Import layering: the engine never imports upward.

`rockycode.engine` is the model- and UI-agnostic core. It may depend on the
foundation modules (config, credentials, pricing, prompts, session storage,
palette, banner) and on itself — never on a host (tui, cli, runtime, server,
headless, mcp_*) or a feature built on top of it (dream, memory, goal,
routines, runners, bench scoring). The check walks every import, lazy ones
inside functions included, so a `from rockycode.dream import …` buried in a
method is caught the same as a top-level one.
"""
from pathlib import Path

from invariants import assert_engine_imports_downward

ROOT = Path(__file__).resolve().parents[1] / "rockycode"

if __name__ == "__main__":
    n = assert_engine_imports_downward(ROOT)
    print(f"layering: {n} engine modules import only downward  ✓")
