"""mdterm: the pure markdown-enhancement functions — clickable file:// links,
per-fence "path links:" lines, tree wrapping at cell width. No Textual, no tty."""
from rich.cells import cell_len

from rockycode.tui.mdterm import (
    _path_link,
    _wrap_tree_or_path_line,
    enhance_markdown,
)

# link targets: absolute paths become clickable file:// URIs (space-safe)
out = enhance_markdown("see [loop.py](/Users/example/proj/loop.py)")
assert "](<file:///Users/example/proj/loop.py>)" in out, out
out = enhance_markdown("see [a b](</Users/example/my proj/a b.md>)")
assert "](<file:///Users/example/my%20proj/a%20b.md>)" in out, out
out = enhance_markdown("[x](/home/du/proj/x.py)")  # not just macOS /Users/
assert "file:///home/du/proj/x.py" in out, out
print("links: abs targets → file:// URIs (space-safe, linux roots too)  ✓")

# `loop.py:128` — the URI drops the line suffix, the label keeps it
link = _path_link("/Users/example/proj/loop.py:128")
assert "[loop.py:128]" in link and "loop.py>" in link and "loop.py:128>" not in link, link
print("links: file.py:128 → clickable file URI, informative label  ✓")

# ...and the same for inline link TARGETS: `[loop.py:128](/path/loop.py:128)`
# must not quote the suffix into a nonexistent `loop.py%3A128` file
out = enhance_markdown("fix in [loop.py:128](/Users/example/proj/loop.py:128)")
assert "](<file:///Users/example/proj/loop.py>)" in out and "%3A" not in out, out
assert "[loop.py:128]" in out, "label must keep the line number: " + out
print("links: :128 target suffix stripped from the URI, kept in the label  ✓")

# markdown-link syntax INSIDE a fence (e.g. a README snippet) stays verbatim
md = "```md\nsee [x](/Users/example/a.py) for details\n```"
out = enhance_markdown(md)
assert "see [x](/Users/example/a.py) for details" in out, out
print("fences: link syntax inside a fence stays verbatim  ✓")

# trailing sentence punctuation is not part of the path
md = "```text\nwrote /Users/example/notes.md.\n```"
out = enhance_markdown(md)
assert "file:///Users/example/notes.md>" in out and "notes.md.>" not in out, out
print("fences: trailing punctuation stripped from collected paths  ✓")

# fences: paths trapped inside get a compact deduped link line AFTER the block
md = "```text\n/Users/example/a.py\n/Users/example/b.py\n/Users/example/a.py\n```"
out = enhance_markdown(md)
lines = out.splitlines()
assert lines[-1].startswith("> path links: "), out
assert lines[-1].count("file://") == 2, "must dedupe: " + lines[-1]
assert "[a.py]" in lines[-1] and "[b.py]" in lines[-1], out
assert "/Users/example/a.py" in out, "fence content itself must stay untouched"
print("fences: path-links line appended after the block, deduped  ✓")

# path-free markdown passes through byte-identical (the no-op guarantee)
md = "plain prose\n\n```py\nx = 1\n```"
assert enhance_markdown(md, width=100) == md
print("no-op: path-free markdown byte-identical  ✓")

# wrap: long tree lines wrap at cell width with a continuation indent
long = ("    ├── /Users/example/Documents/Code/rockycode-researchfunc/"
        "rockycode/engine/very_long_module_name.py")
rows = _wrap_tree_or_path_line(long, 48)
assert len(rows) > 1, rows
assert all(cell_len(r) <= 48 for r in rows), rows
assert rows[1].startswith("      "), rows  # original indent + 2
print("wrap: tree/path lines wrap at width, indent carried  ✓")

# CJK: width is measured in CELLS (CJK chars are 2 wide), not characters
cjk = "├── 项目文件/研究记录/中文目录/说明文档/示例输出/结果文本/最终版本.md"
rows = _wrap_tree_or_path_line(cjk, 30)
assert len(rows) > 1 and all(cell_len(r) <= 30 for r in rows), rows
print("wrap: CJK measured by cells  ✓")

# code-looking lines are spared — wrapping code would corrupt it
code = "    x = do_thing(arg_one, arg_two, arg_three, arg_four, arg_five, arg_six)"
assert _wrap_tree_or_path_line(code, 40) == [code]
print("wrap: code-like lines untouched  ✓")

# end to end: a fence with a long tree wraps AND gets its links line
md = "```\n" + long + "\n```"
out = enhance_markdown(md, width=56)  # fence wrap width = min(56-8, 88) = 48
body = out.splitlines()
assert any(r.startswith("> path links: ") for r in body), out
in_fence_rows = body[1:body.index("```", 1)]
assert len(in_fence_rows) > 1 and all(cell_len(r) <= 48 for r in in_fence_rows), out
out_nowrap = enhance_markdown(md)  # width=None → links yes, wrapping no
assert long in out_nowrap.splitlines(), out_nowrap
print("end-to-end: fence wrapped at width, links line present; width=None wraps nothing  ✓")

# ── the render layer: what the PARSER does with our output ──────────────────
# markdown-it's stock security filter drops file: hrefs — the link would show
# as raw `](<file:///…>)` text. rocky_markdown_parser must keep it a real link.
from rockycode.tui.mdterm import link_click_action, rocky_markdown_parser


def hrefs_of(text):
    return [
        c.attrs.get("href")
        for t in rocky_markdown_parser().parse(text) if t.type == "inline"
        for c in (t.children or []) if c.type == "link_open"
    ]

out = enhance_markdown("fix in [loop.py:128](/Users/example/proj/loop.py:128)")
assert hrefs_of(out) == ["file:///Users/example/proj/loop.py"], (out, hrefs_of(out))
assert "](<" not in "".join(
    c.content
    for t in rocky_markdown_parser().parse(out) if t.type == "inline"
    for c in (t.children or []) if c.type == "text"
), "raw link syntax must not leak into visible text"
print("render: file:// survives the parser as a real link, no raw-text leak  ✓")

# fuzzy linkify off: `loop.py` is a file, not a Paraguayan website —
# but explicit https:// URLs still autolink
assert hrefs_of("edited loop.py and app.py today") == []
assert hrefs_of("see https://arxiv.org/abs/2301.00001") == ["https://arxiv.org/abs/2301.00001"]
print("render: no http://loop.py fuzzy links; real URLs still autolink  ✓")

# ── click policy: check things, never run things ─────────────────────────────
import tempfile
from pathlib import Path as _P

tmp = _P(tempfile.mkdtemp(prefix="mdterm-click-"))
paper = tmp / "attention is all.md"; paper.write_text("# hi")
script = tmp / "run.py"; script.write_text("print('no')")

assert link_click_action("https://arxiv.org/abs/1706.03762")[0] == "browser"
action, target = link_click_action(paper.as_uri())          # %20-encoded href
# target is the RESOLVED uri (macOS /var → /private/var): open what was vetted
assert action == "open" and target == paper.resolve().as_uri(), (action, target)
assert link_click_action(script.as_uri())[0] == "blocked"    # code never opens
assert link_click_action((tmp / "gone.pdf").as_uri())[0] == "missing"
assert link_click_action("file:///Users/x/setup.sh")[0] == "blocked"
assert link_click_action("javascript:alert(1)")[0] == "blocked"
print("click: md/pdf+web open, code/shell blocked, moved files reported  ✓")

# HARD RULE: nothing executable ever opens — judged on the RESOLVED target,
# because macOS `open` follows symlinks and would RUN a .command/+x target
import os as _os

trap = tmp / "trap.md"; trap.write_text("#!/bin/sh\necho pwned")
_os.chmod(trap, 0o755)                                    # exec bit → never
assert link_click_action(trap.as_uri())[0] == "blocked", "exec bit must block"
sneaky = tmp / "innocent.md"; sneaky.symlink_to(script)   # .md → run.py
assert link_click_action(sneaky.as_uri())[0] == "blocked", "symlink judged by target"
honest = tmp / "alias.md"; honest.symlink_to(paper)       # .md → real doc
assert link_click_action(honest.as_uri())[0] == "open"
folder = tmp / "notes.md"; folder.mkdir()                 # dir in md clothing
assert link_click_action(folder.as_uri())[0] == "blocked", "non-regular files stay closed"
assert link_click_action((tmp / "sub.md").as_uri())[0] == "missing"
print("click: HARD RULE — exec bit and symlinks-to-code never open  ✓")

# only_existing: fence links only for paths that exist on THIS machine
# (space-free name: bare-path capture stops at whitespace by design)
doc = tmp / "paper.md"; doc.write_text("# hi")
md = f"```text\n{doc}\n/Users/nobody/imaginary.md\n```"
out = enhance_markdown(md, only_existing=True)
links_line = [l for l in out.splitlines() if l.startswith("> path links: ")]
assert len(links_line) == 1 and "imaginary" not in links_line[0], out
assert "paper.md" in links_line[0], out
out = enhance_markdown("```text\n/Users/nobody/imaginary.md\n```", only_existing=True)
assert "> path links:" not in out, "all-dead fence must get no links line: " + out
print("only_existing: dead/renamed paths get no link, real ones do  ✓")

print("MDTERM SMOKE OK — paths you can click, trees that fit. amaze!")
