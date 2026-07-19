"""Explore primitive: the child registry is read-only BY CONSTRUCTION, the
read-only bash gate refuses everything that isn't an allowlisted in-tree read,
the citation checker verifies `path:line "anchor"` evidence mechanically (incl.
against a git ref for branch reviews), and review_goal_branch delegates to a
wired reviewer instead of dumping the diff. Fake DeepSeek stream, no network.

The load-bearing invariant (see engine/explore.py docstring): the `explore`
tool is risk="safe" so purchases fan out through loop.py's read-parallel batch
path, while any future WRITER role must register "risky" and thereby
serialize — parallel writers stay impossible by construction, not by prompt.
"""
import asyncio
import subprocess
import tempfile
import types
from pathlib import Path

# On main: foundation + chat explore tool (buyer 1) + goal branch reviewer
# (buyer 2, git-ref reads) + goal verifier wired into EngineDriver (buyer 3).
from rockycode.engine.goal_review import build_goal_tools
from rockycode.engine.explore import (
    ROLE_PROMPTS,
    _ro_bash_check,
    build_explore_registry,
    build_explore_tool,
    check_citations,
    make_branch_reviewer,
    make_goal_verifier,
    run_explore,
)


def test_registry_shape():
    with tempfile.TemporaryDirectory() as td:
        reg = build_explore_registry(Path(td))
    for name in ("read_file", "grep", "glob", "check_code", "bash"):
        assert name in reg, f"child registry missing {name}: {sorted(reg)}"
    for name in ("write_file", "edit_file", "explore", "agent"):
        assert name not in reg, f"child registry must not carry {name}"
    bad = {n: t.risk for n, t in reg.items() if t.risk != "safe"}
    assert not bad, f"child tools must all be safe-tier (read-only): {bad}"
    print("child registry: reads + gated bash, no writers, no recursion  ✓")


def test_ro_bash_gate():
    allowed = [
        "git diff",
        "git diff HEAD...goal/20260706-2113",
        "git log --oneline HEAD..goal/20260706-2113",
        "git show goal/20260706-2113:rockycode/cli.py",
        "git show HEAD~1 --stat",
        "git branch --list",
        "ls -la rockycode",
        "head -80 rockycode/cli.py | wc -l",
        "du -sh .",
        "git diff | head -100",
    ]
    for cmd in allowed:
        assert _ro_bash_check(cmd) is None, f"should be allowed: {cmd}"

    blocked = [
        "rm -rf /",                       # not allowlisted
        "git push origin main",           # mutating git subcommand
        "git remote add evil http://x",   # not allowlisted subcommand
        "git branch -D main",             # branch with mutating flag
        "git -C /etc log",                # -C is not a subcommand → refused
        "echo hi > pwned",                # redirect (and echo not allowlisted)
        "wc -l < /etc/passwd",            # `<` opens arbitrary files
        "git diff --output=pwned",        # in-tree write via flag
        "ls; rm -rf .",                   # chaining
        "ls && rm -rf .",                 # chaining
        "ls & ",                          # backgrounding
        "ls `rm -rf .`",                  # substitution
        "ls $(rm -rf .)",                 # substitution
        "head $HOME/.ssh/id_rsa",         # `$` expansion
        "FOO=1 ls",                       # env assignment names no binary
        "head ~/.ssh/id_rsa",             # ~ escape
        "head /etc/passwd",               # absolute path escape
        "head ../../secrets.txt",         # .. escape
        "head .env",                      # in-tree secret file
        "tail id_rsa",                    # in-tree secret file
        "ls\nrm -rf .",                   # multi-line
        'ls "unclosed',                   # unbalanced quotes → refuse, not guess
        "sort -o pwned pwned",            # not allowlisted (writes via -o)
        "find . -delete",                 # not allowlisted
    ]
    for cmd in blocked:
        assert _ro_bash_check(cmd) is not None, f"should be BLOCKED: {cmd}"
    print(f"ro-bash gate: {len(allowed)} reads pass, {len(blocked)} escapes refused  ✓")


def sh(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)


def new_repo():
    d = Path(tempfile.mkdtemp())
    sh(d, "init", "-q")
    sh(d, "config", "user.email", "t@t")
    sh(d, "config", "user.name", "t")
    (d / "app.py").write_text("import os\n\n\ndef main():\n    return os.getcwd()\n")
    sh(d, "add", "-A")
    sh(d, "commit", "-q", "-m", "init")
    return d.resolve()


async def test_citation_checker():
    repo = new_repo()

    # Exact line, off-by-two (window), bad anchor, missing file, bare line.
    # Absolute / `..` paths never parse as citations (the regex requires a
    # word-char start + dot-extension), so `/etc/passwd:1` below contributes
    # nothing — cited paths are always interpreted repo-relative and jailed.
    report = (
        'FINDINGS: fine.\nEVIDENCE:\n'
        '- app.py:4 "def main():"\n'          # actual line 4 ✓
        '- app.py:2 "import os"\n'            # actually line 1 — within ±3 ✓
        '- app.py:4 "def totally_else():"\n'  # anchor nowhere ✗
        '- gone.py:1 "x"\n'                   # file not found ✗
        '- /etc/passwd:1 "root"\n'            # not a parseable citation → ignored
        'Also touches app.py:5 in passing.\n' # bare (no anchor): line exists ✓
    )
    footer = await check_citations(report, workdir=repo)
    assert footer.startswith("[citations: 3/5 verified"), footer
    assert "anchor not found in file" in footer, footer
    assert "file not found" in footer, footer
    assert "passwd" not in footer, footer
    print("citation checker: exact + fuzzy verified; bad anchor/missing file flagged  ✓")

    # E2E-observed deviations: under-qualified paths resolve iff unique; a real
    # quote with a stale line number verifies (fabrication is the target, not
    # miscounting); an ambiguous short path stays unverified.
    (repo / "pkg").mkdir()
    (repo / "pkg" / "deep.py").write_text("x = 1\nSENTINEL = 'here'\n")
    short = '- deep.py:1 "SENTINEL = \'here\'"'      # short path + stale line
    ok = await check_citations(short, workdir=repo)
    assert ok == "[citations: 1/1 verified]", ok
    (repo / "pkg2").mkdir()
    (repo / "pkg2" / "deep.py").write_text("other\n")
    ambig = await check_citations(short, workdir=repo)
    assert "0/1 verified" in ambig and "file not found" in ambig, ambig
    print("citation checker: unique short paths resolve; ambiguous ones refused  ✓")

    none = await check_citations("FINDINGS: trust me, it's fine.", workdir=repo)
    assert none.startswith("[citations: none found"), none
    print("citation checker: evidence-free report gets the caution footer  ✓")

    # Branch-only content verifies via git_ref (the branch is NOT checked out).
    sh(repo, "checkout", "-q", "-b", "goal/t1")
    (repo / "app.py").write_text("import sys\n\n\ndef main():\n    return sys.argv\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "goal work")
    sh(repo, "checkout", "-q", "-")
    branch_cite = '- app.py:5 "return sys.argv"'
    miss = await check_citations(branch_cite, workdir=repo)
    assert "0/1 verified" in miss, miss
    hit = await check_citations(branch_cite, workdir=repo, git_ref="goal/t1")
    assert hit == "[citations: 1/1 verified]", hit
    print("citation checker: branch-review citations verify via git show ref:path  ✓")

    # Model-deviation tolerance (all seen in a real E2E run): backticked path,
    # a "(branch)" marker before the colon, and a line range.
    sloppy = '- `app.py` (branch):4-5 "def main():"'
    tol = await check_citations(sloppy, workdir=repo, git_ref="goal/t1")
    assert tol == "[citations: 1/1 verified]", tol
    print("citation checker: tolerates backticks, (branch) markers, line ranges  ✓")

    # A bare (no-anchor) citation to a line that only exists on the branch
    # (branch app.py is longer): good if it holds on EITHER side.
    (repo / "app.py").write_text("import os\n")  # tree copy now 1 line
    bare = "see app.py:5 for the return"
    assert "0/1" in await check_citations(bare, workdir=repo), "tree-only should miss"
    ok = await check_citations(bare, workdir=repo, git_ref="goal/t1")
    assert ok == "[citations: 1/1 verified]", ok
    sh(repo, "checkout", "-q", "--", "app.py")
    print("citation checker: bare citations fall back to the reviewed ref too  ✓")


class _FakeTrajectory:
    session_id = "fake-child-session"


class _FakeEngine:
    """Test seam (engine_cls): scripted child — one 'turn', canned report.
    Records its construction so the wiring is assertable."""

    last = None
    report = 'FINDINGS: flag found.\nEVIDENCE:\n- app.py:4 "def main():"'

    def __init__(self, **kw):
        self.kw = kw
        self.history = [
            {"role": "system", "content": kw.get("system_prompt", "")},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c0"}]},
            {"role": "tool", "tool_call_id": "c0", "content": "[exit 0]"},
            {"role": "assistant", "content": _FakeEngine.report},
        ]
        self.trajectory = _FakeTrajectory()
        _FakeEngine.last = self

    async def run_turn(self, message):
        from rockycode.engine.events import TurnFinished
        self.message = message
        yield TurnFinished(steps=3, usage={"prompt_tokens": 1200, "completion_tokens": 80})


class _FakeLedger:
    def __init__(self):
        self.calls = []

    def add(self, model, usage):
        self.calls.append((model, usage))


async def test_run_explore():
    repo = new_repo()
    ledger = _FakeLedger()
    out = await run_explore(
        task="Where is the feature flag defined?",
        role="explore",
        context="probably app.py",
        model="deepseek-v4",
        client=None,
        workdir=repo,
        ledger=ledger,
        parent_session="parent-123",
        engine_cls=_FakeEngine,
    )
    assert "FINDINGS: flag found." in out, out
    assert "[citations: 1/1 verified]" in out, out
    assert "[explore:explore — 3 steps" in out and "fake-child-session" in out, out
    assert ledger.calls == [("deepseek-v4", {"prompt_tokens": 1200, "completion_tokens": 80})]

    child = _FakeEngine.last
    assert child.kw["system_prompt"] == ROLE_PROMPTS["explore"]
    assert child.kw["max_steps"] == 20 and child.kw["reasoning_effort"] == "high"
    assert child.kw["trajectory_meta"]["source"] == "explore"
    assert child.kw["trajectory_meta"]["parent_session"] == "parent-123"
    assert "Context from the buyer:" in child.message
    print("run_explore: report + verified-citation footer + stats, ledger fed  ✓")

    bad = await run_explore(
        task="x", role="deploy", model="m", client=None, workdir=repo,
        engine_cls=_FakeEngine,
    )
    assert bad.startswith("[error] unknown explore role"), bad
    print("unknown role refused with the valid role list  ✓")


async def test_goal_review_delegation():
    repo = new_repo()
    sh(repo, "checkout", "-q", "-b", "goal/t2")
    (repo / "app.py").write_text("import os\n\n\ndef main():\n    return os.sep\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "goal work t2")
    sh(repo, "checkout", "-q", "-")

    calls = []

    async def fake_reviewer(branch):
        calls.append(branch)
        return "VERDICT: merge-ready.\n- app.py:5 \"return os.sep\""

    tools = build_goal_tools(workdir=repo, reviewer=fake_reviewer)
    review = tools["review_goal_branch"].fn

    out = await review(branch="goal/t2")
    assert "delegated review of goal/t2" in out and "VERDICT: merge-ready." in out, out
    assert "diff --git" not in out, "raw diff leaked into the delegated review"
    assert calls == ["goal/t2"]
    print("review_goal_branch: delegates to the reviewer — no diff in context  ✓")

    out = await review(branch="goal/t2", raw=True)
    assert "diff --git" in out and "VERDICT" not in out, out
    print("review_goal_branch: raw=true still returns the plain diff  ✓")

    async def broken_reviewer(branch):
        raise RuntimeError("model down")

    tools = build_goal_tools(workdir=repo, reviewer=broken_reviewer)
    out = await tools["review_goal_branch"].fn(branch="goal/t2")
    assert out.startswith("[note] delegated review failed (RuntimeError)"), out
    assert "diff --git" in out, "fallback must still show the diff"
    print("review_goal_branch: reviewer failure degrades to the raw dump  ✓")

    tools = build_goal_tools(workdir=repo)  # no reviewer → unchanged old behavior
    out = await tools["review_goal_branch"].fn(branch="goal/t2")
    assert out.startswith("[ok] diff of goal/t2") and "diff --git" in out, out
    print("review_goal_branch: without a reviewer the old dump path is intact  ✓")

    # Guards still hold on the new signature.
    out = await review(branch="main")
    assert out.startswith("[refused]"), out
    out = await review(branch="goal/nope")
    assert out.startswith("[not found]"), out
    print("review_goal_branch: non-goal ref and missing branch still refused  ✓")


async def test_goal_verifier():
    repo = new_repo()

    async def judge(report):
        """Run make_goal_verifier with a scripted child report."""
        _FakeEngine.report = report
        v = make_goal_verifier(client=None, model="m", workdir=repo, engine_cls=_FakeEngine)
        return await v(milestone="add main()", summary="did it", baseline="", checks="clean")

    ok, note = await judge('PASS — main() exists.\n- app.py:4 "def main():"')
    assert ok and "citations: 1/1 verified" in note, note
    child = _FakeEngine.last
    assert child.kw["system_prompt"] == ROLE_PROMPTS["verify"]
    assert child.kw["max_steps"] == 12
    assert "do NOT trust it" in child.message and "regression" in child.message
    print("goal verifier: PASS verdict + verified citation, verify role wired  ✓")

    ok, _ = await judge("FAIL — claim is false, no such change on disk.")
    assert not ok
    # Dressed verdicts (seen in E2E) still parse instead of conservatively failing.
    ok, _ = await judge("VERDICT: **PASS** — all good.")
    assert ok, "dressed PASS verdict must parse as PASS"
    print("goal verifier: FAIL and dressed verdicts parsed correctly  ✓")

    try:
        await judge("I looked around and things seem plausible overall.")
        raise AssertionError("no-verdict report must raise for fallback")
    except RuntimeError as e:
        assert "no clean PASS/FAIL" in str(e)
    print("goal verifier: verdict-free report raises → caller falls back  ✓")


async def test_engine_driver_grounded_verify():
    from rockycode.engine.goal import EngineDriver

    repo = new_repo()
    ws = types.SimpleNamespace(path=repo)

    class _OneShot:
        """Non-streaming fake for EngineDriver._call (the fallback judge)."""
        class chat:
            class completions:
                @staticmethod
                async def create(**kw):
                    msg = types.SimpleNamespace(content="PASS\nfallback judge spoke")
                    return types.SimpleNamespace(
                        usage=None, choices=[types.SimpleNamespace(message=msg)])

    async def good_verifier(**kw):
        return False, "FAIL — grounded verifier spoke"

    d = EngineDriver(client=_OneShot(), model="m", reviewer_model="m",
                     workspace=ws, ledger=_FakeLedger(), verifier=good_verifier)
    ok, note = await d.verify("m1", "worker says done")
    assert not ok and note == "FAIL — grounded verifier spoke"
    print("EngineDriver.verify: grounded verifier wins when wired  ✓")

    async def broken_verifier(**kw):
        raise RuntimeError("no clean PASS/FAIL verdict")

    d = EngineDriver(client=_OneShot(), model="m", reviewer_model="m",
                     workspace=ws, ledger=_FakeLedger(), verifier=broken_verifier)
    ok, note = await d.verify("m1", "worker says done")
    assert ok and "fallback judge spoke" in note
    print("EngineDriver.verify: verifier failure degrades to the summary judge  ✓")

    d = EngineDriver(client=_OneShot(), model="m", reviewer_model="m",
                     workspace=ws, ledger=_FakeLedger())
    ok, _ = await d.verify("m1", "worker says done")
    assert ok, "no verifier → old path must be intact"
    print("EngineDriver.verify: without a verifier the old judge is intact  ✓")


def test_explore_tool_contract():
    eng = types.SimpleNamespace(
        model="deepseek-v4", client=None, workdir=Path("/tmp"), allowed_roots=(),
        thinking=True, max_tokens=384_000, trajectory=_FakeTrajectory(),
    )
    tool = build_explore_tool(eng)["explore"]
    # THE invariant: safe tier → read-parallel fan-out; a writer role must flip this.
    assert tool.risk == "safe", "explore must be safe-tier (read-only child)"
    fn = tool.schema["function"]
    assert fn["name"] == "explore"
    assert fn["parameters"]["required"] == ["task", "role"]
    assert fn["parameters"]["properties"]["role"]["enum"] == ["explore", "review"]
    # make_branch_reviewer builds from the same engine surface.
    assert callable(make_branch_reviewer(eng))
    print("explore tool: safe-tier, schema contract (task+role required, role enum)  ✓")


async def main():
    test_registry_shape()
    test_ro_bash_gate()
    await test_citation_checker()
    await test_run_explore()
    await test_goal_review_delegation()
    await test_goal_verifier()
    await test_engine_driver_grounded_verify()
    test_explore_tool_contract()
    print("EXPLORE SMOKE OK — verified findings bought, diffs stay out of context. amaze!")


asyncio.run(main())
