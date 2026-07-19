"""Smoke test for grep/glob in both registries (local + session). No Docker."""
import asyncio
import tempfile
from pathlib import Path

from rockycode.engine import tools as tools_mod
from rockycode.engine.container import LocalSession, build_session_registry


async def main():
    workdir = Path(tempfile.mkdtemp(prefix="rockytools-"))
    (workdir / "src").mkdir()
    (workdir / "src" / "app.py").write_text("def handler():\n    return compute_total(1, 2)\n")
    (workdir / "src" / "math_util.py").write_text("def compute_total(a, b):\n    return a + b\n")
    (workdir / "README.md").write_text("compute_total docs\n")
    (workdir / "__pycache__").mkdir()
    (workdir / "__pycache__" / "junk.py").write_text("compute_total junk\n")
    (workdir / "blob.bin").write_bytes(b"compute_total\0binary")

    for label, registry in [
        ("local", tools_mod.build_registry(workdir)),
        ("session", build_session_registry(LocalSession(workdir))),
    ]:
        out, ok = await tools_mod.execute(
            registry, "grep", '{"pattern": "compute_total", "include": "*.py"}'
        )
        assert ok, (label, out)
        assert "math_util.py" in out and "app.py" in out, (label, out)
        assert "junk" not in out, (label, "junk dir not skipped: " + out)
        assert "blob.bin" not in out, (label, "binary not skipped: " + out)

        out, ok = await tools_mod.execute(registry, "grep", '{"pattern": "no_such_symbol_xyz"}')
        assert "[no matches]" in out, (label, out)

        out, ok = await tools_mod.execute(registry, "glob", '{"pattern": "**/*.py"}')
        assert ok, (label, out)
        assert "src/app.py" in out and "src/math_util.py" in out, (label, out)

        out, ok = await tools_mod.execute(registry, "grep", '{"pattern": "(unclosed"}')
        assert not ok and "[error]" in out, (label, out)

        print(f"{label} registry: grep + glob ok")

    print("TOOLS SMOKE OK — amaze amaze amaze!")


asyncio.run(main())
