"""Doc dock: read a file BESIDE the chat — never over it, never in history.

Pure part: read_doc (cap, .txt fencing) + the suffix routing set.
Pilot part (headless Textual, fake engine, no API): open docks beside the
transcript, relative links inside a docked doc navigate through the same
click policy, back/width/full/esc all behave, close restores exact width.
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.chdir(tempfile.mkdtemp(prefix="rockymdview-"))

from rockycode.tui.mdview import TEXT_DOC_SUFFIXES, DocDock, read_doc

# ── pure: read_doc ───────────────────────────────────────────────────────────
p = Path("doc.md"); p.write_text("# title\nbody")
assert read_doc(p).startswith("# title")
big = Path("big.md"); big.write_text("y" * 300_000)
out = read_doc(big)
assert len(out) < 300_000 and "first 200,000 characters" in out, out[-80:]
t = Path("note.txt"); t.write_text("has `ticks` and *stars*")
assert read_doc(t).startswith("````text"), "txt must stay verbatim in a fence"
assert ".md" in TEXT_DOC_SUFFIXES and ".py" not in TEXT_DOC_SUFFIXES
print("read_doc: cap honest, txt fenced, routing set sane  ✓")

# ── pilot: the dock beside the chat ──────────────────────────────────────────
from rockycode.engine.loop import Engine
from rockycode.tui.app import RockyCodeApp


def build_app():
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace()))
    eng = Engine(model="fake", client=client, workdir=Path.cwd())
    return RockyCodeApp(eng, permission="yolo")


async def main():
    doc1 = Path("one.md").resolve(); doc1.write_text("# one\n[next](two.md)")
    doc2 = Path("two.md").resolve(); doc2.write_text("# two")
    app = build_app()
    async with app.run_test(size=(120, 40)) as pilot:
        transcript = app.query_one("#transcript")
        full_w = transcript.size.width

        await app._open_doc(doc1.as_uri())
        await pilot.pause()
        dock = app.query_one(DocDock)
        assert dock.current == doc1
        assert transcript.display and transcript.size.width < full_w, \
            "beside the chat, not over it"

        # relative link inside the docked doc → same policy gate → navigates
        await app.on_markdown_link_clicked(types.SimpleNamespace(href="two.md"))
        await pilot.pause()
        assert dock.current == doc2, dock.current
        await app.action_doc_back()
        assert dock.current == doc1

        # a docked doc's link to CODE still refuses (dim note, no dock change)
        code = Path("run.py").resolve(); code.write_text("print('no')")
        await app.on_markdown_link_clicked(types.SimpleNamespace(href="run.py"))
        await pilot.pause()
        assert dock.current == doc1, "code must not dock or open"

        app.action_doc_wider()   # cycles a width preset without error
        app.action_doc_full()
        await pilot.pause()
        assert not transcript.display, "⛶ full hides the chat column"
        app.action_doc_full()
        await pilot.pause()
        assert transcript.display

        await app.action_cancel_turn()  # esc, no turn running → closes dock
        await pilot.pause()
        assert not app.query(DocDock), "esc must close the dock"
        assert transcript.display and transcript.size.width == full_w, \
            "exact single-column view restored"
    print("dock: beside-not-over, policy-gated nav, back/width/full, esc-close  ✓")


asyncio.run(main())
print("MDVIEW SMOKE OK — papers beside the chat, history undisturbed. amaze!")
