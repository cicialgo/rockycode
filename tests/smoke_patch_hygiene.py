"""extract_patch hygiene: the three score-loss guards from the flip analysis.

Real local git repo + LocalSession — no docker, no network. Each scenario is
a failure mode that cost a resolved task in the two-round DeepSeek runs:
stashed work (sphinx-8548), binary pollution (matplotlib-24570), and
model-authored test files colliding with the gold test patch (django-16877).
"""
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from rockycode.engine.container import LocalSession, extract_patch


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _fresh_repo() -> Path:
    repo = Path(tempfile.mkdtemp(prefix="rockypatch-"))
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "rocky@test")
    _git(repo, "config", "user.name", "rocky")
    (repo / "src.py").write_text("def f():\n    return 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_src.py").write_text("def test_f():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


async def main() -> None:
    # 1 — stashed work is popped back and lands in the patch
    repo = _fresh_repo()
    (repo / "src.py").write_text("def f():\n    return 2\n")
    _git(repo, "stash")
    patch = await extract_patch(LocalSession(repo))
    assert "return 2" in patch, "stashed fix must be recovered into the patch"
    assert "src.py" in patch
    print("hygiene: `git stash`ed fix is popped and extracted  ✓")

    # 2 — binary junk is dropped; the real edit survives
    repo = _fresh_repo()
    (repo / "src.py").write_text("def f():\n    return 3\n")
    (repo / "repro.png").write_bytes(bytes(range(256)) * 4)
    patch = await extract_patch(LocalSession(repo))
    assert "return 3" in patch, "the source edit must survive the binary drop"
    assert "repro.png" not in patch, "binary junk must not ride into the patch"
    assert "Binary files" not in patch
    print("hygiene: binary scratch files dropped, source edit kept  ✓")

    # 3 — test-file edits and new test files are stripped (incl. conftest)
    repo = _fresh_repo()
    (repo / "src.py").write_text("def f():\n    return 4\n")
    (repo / "tests" / "test_src.py").write_text("def test_f():\n    assert False\n")
    (repo / "tests" / "test_new.py").write_text("def test_new():\n    pass\n")
    (repo / "conftest.py").write_text("collect_ignore = []\n")
    patch = await extract_patch(LocalSession(repo))
    assert "return 4" in patch, "the source edit must survive the test strip"
    assert "test_src.py" not in patch and "test_new.py" not in patch, \
        "test edits must be stripped — the scorer applies its OWN test patch"
    assert "conftest.py" not in patch
    print("hygiene: test-file edits + new test files stripped from the patch  ✓")

    # 4 — clean tree → empty patch, and extraction is idempotent
    repo = _fresh_repo()
    assert (await extract_patch(LocalSession(repo))).strip() == ""
    (repo / "src.py").write_text("def f():\n    return 5\n")
    s = LocalSession(repo)
    first, second = await extract_patch(s), await extract_patch(s)
    assert first == second and "return 5" in first, "extraction must be idempotent"
    print("hygiene: clean tree → empty; extraction idempotent  ✓")


asyncio.run(main())
print("PATCH HYGIENE SMOKE OK — no more losing solved tasks to junk. amaze!")
