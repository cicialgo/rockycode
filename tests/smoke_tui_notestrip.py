"""The note strip in a real Textual app: this project's tasks as one row of
clickable notes (`♪ 01 title ✉1`). Offline: a fake engine, a throwaway home.

It must stay out of an ordinary chat, follow new tasks and mail, take the same
path as `/task open` when clicked, keep the current task visible when the
terminal is narrow, and cut titles by terminal cells."""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

_HOME = tempfile.TemporaryDirectory(prefix="rocky-notes-home-")
os.environ["ROCKYCODE_HOME"] = _HOME.name
from rich.cells import cell_len  # noqa: E402

from rockycode import session as session_mod  # noqa: E402
from rockycode.engine.loop import Engine  # noqa: E402
from rockycode.task_links import TaskLinks  # noqa: E402
from rockycode.tui import notestrip  # noqa: E402
from rockycode.tui.app import RockyCodeApp  # noqa: E402
from rockycode.tui.notestrip import NoteStrip, TaskNote  # noqa: E402
from rockycode.tui.tasks import TaskSwitch  # noqa: E402


def build(root, info=None, ui_language="en"):
    engine = Engine(model="fake", client=NS(), registry={}, workdir=root, thinking=False)
    app = RockyCodeApp(engine, resume_session=info, evolve="off", ui_language=ui_language)
    engine.hooks.clear()
    return app


def texts(app):
    return [str(note.render()) for note in app.query(TaskNote)]


def pure():
    row = lambda number, title, waiting=0: {"id": f"t{number}", "title": title, "number": number, "waiting": waiting}  # noqa: E731
    assert notestrip.label(row(1, "fix login")) == "♪ 01 fix login"
    assert notestrip.label(row(2, "write tests", 3)) == "♫ 02 write tests ✉3"
    assert notestrip.label(row(5, "")) == "♪ 05"  # the four notes repeat, the number does not
    assert notestrip.label(row(12, "x")) == "♩ 12 x"
    # Cells, not characters: a CJK title is twice as wide as its length.
    assert notestrip.shorten("整理田忌赛马的实验记录并补上结论") == "整理田忌赛马的实…"
    assert cell_len(notestrip.shorten("整理田忌赛马的实验记录并补上结论")) <= notestrip.TITLE_CELLS
    assert notestrip.shorten("two\nlines   here") == "two lines here"
    rows = [row(n, f"task number {n}") for n in range(1, 9)]
    shown, hidden = notestrip.fit(rows, "t8", 200)
    assert (len(shown), hidden) == (8, 0)
    shown, hidden = notestrip.fit(rows, "t8", 60)
    assert hidden and shown[-1]["id"] == "t8" and [r["number"] for r in shown] == sorted(r["number"] for r in shown)
    width = sum(cell_len(notestrip.label(r)) for r in shown) + notestrip.GAP * len(shown) + cell_len(f"+{hidden}")
    assert width <= 60 - 2 * notestrip.PAD, width
    shown, hidden = notestrip.fit(rows, "t1", 60)  # an old current task keeps a place
    assert "t1" in [r["id"] for r in shown] and hidden
    shown, hidden = notestrip.fit(rows, "t1", 20)  # too narrow for two: the current one stays
    assert [r["id"] for r in shown] == ["t1"] and hidden == 7
    print("labels, cell-width titles and fitting  ✓")


async def main():
    pure()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        app = build(root)
        async with app.run_test(size=(110, 36)) as pilot:
            strip = app.query_one(NoteStrip)
            assert not strip.has_class("shown") and not strip.display, "an ordinary chat has no strip"
            assert not app._task_session.registered, "drawing the strip must not make the chat a task"
            source = app._task_session.id
            first = json.loads(await app.engine.registry["create_new_task"].fn(prompt="write the tests"))["task_id"]
            second = json.loads(await app.engine.registry["create_new_task"].fn(prompt="update the docs"))["task_id"]
            await pilot.pause()
            assert strip.has_class("shown") and strip.display
            assert texts(app) == ["♪ 01", "♫ 02 write the tests", "♬ 03 update the docs"], texts(app)
            notes = list(app.query(TaskNote))
            assert [note.task_id for note in notes] == [source, first, second]
            assert [note.here for note in notes] == [True, False, False]
            assert notes[0].has_class("here") and notes[0].tooltip == app._t("tasks.strip_here")
            assert notes[1].tooltip == app._t("tasks.strip_open", n="02"), "the tooltip teaches the command"
            print("three tasks → three notes, numbered in creation order, the current one marked  ✓")

            # The session's own name (given after its first turn) names an untitled task's note.
            app._task_session.retitle("fix the login redirect loop for SSO")
            await pilot.pause()
            assert texts(app)[0] == "♪ 01 fix the login red…", texts(app)
            app._task_session.retitle("a later name never replaces it")
            await pilot.pause()
            assert texts(app)[0] == "♪ 01 fix the login red…"

            # Mail waiting in a task shows on that task's note, and goes when it is read and dismissed.
            await app.engine.registry["send_task_message"].fn(target=first, message="one question")
            await pilot.pause()
            assert texts(app)[1] == "♫ 02 write the tests ✉1", texts(app)
            assert app._t("tasks.strip_mail", n=1) in list(app.query(TaskNote))[1].tooltip
            other = TaskLinks(Path(_HOME.name) / "task-links.sqlite3")  # another terminal
            other.send(first, source, "an answer")
            app.refresh_notes()
            await pilot.pause()
            assert texts(app)[0] == "♪ 01 fix the login red… ✉1", texts(app)
            await app._handle_slash(f'/task dismiss {other.inbox(source)[0]["id"]}')
            await pilot.pause()
            assert texts(app)[0] == "♪ 01 fix the login red…", texts(app)
            other.close()
            print("mail badge follows the inbox, from this terminal and from another  ✓")

            # A narrow terminal keeps the current task and says how many are left out.
            await pilot.resize_terminal(40, 30)
            await pilot.pause()
            narrow = list(app.query(TaskNote))
            assert narrow[0].task_id is None and str(narrow[0].render()).startswith("+"), texts(app)
            assert any(note.here for note in narrow), "the current task always stays"
            await pilot.resize_terminal(110, 36)
            await pilot.pause()
            assert len(texts(app)) == 3

            # A click is `/task open <id>`: refused while busy, the ordinary exit flow otherwise.
            said = []
            original = type(app._task_session).say
            app._task_session.say = lambda text: said.append(text) or asyncio.sleep(0)
            app._delivery.paused = True
            from rockycode.engine.delivery import TurnMessage
            app._delivery.pending.append(TurnMessage("queued"))
            await pilot.click(list(app.query(TaskNote))[2])
            await app.workers.wait_for_complete()
            assert said[-1] == app._t("tasks.busy") and app.return_value is None
            await app._delivery.clear()
            app._task_session.say = original.__get__(app._task_session)
            await pilot.click(list(app.query(TaskNote))[0])  # the current note goes nowhere
            await pilot.pause()
            assert app._switch_target is None
            await pilot.click(list(app.query(TaskNote))[2])
            await app.workers.wait_for_complete()
        switched = app.return_value
        assert isinstance(switched, TaskSwitch) and switched.info.session_id, "a click switches like /task open"
        print("click = /task open: busy guard, then the exit flow with the chosen task  ✓")

        # The task it switched to opens with the same strip, itself marked; Chinese tooltips.
        child = build(root, switched.info, ui_language="zh")
        async with child.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            notes = list(child.query(TaskNote))
            assert [note.task_id for note in notes] == [source, first, second]
            assert [note.here for note in notes] == [False, False, True]
            assert notes[2].tooltip == "当前任务" and "/task open 01" in notes[0].tooltip
            # Without a mouse, the number on the note is enough: `/task open 2`.
            said = []
            child._task_session.say = lambda text: said.append(text) or asyncio.sleep(0)
            await child._handle_slash("/task open 9")  # no such number: the ordinary refusal
            assert said[-1] == child._t("tasks.err_unknown_task")
            await child._handle_slash("/task open 02")
            await child.workers.wait_for_complete()
        assert isinstance(child.return_value, TaskSwitch)
        assert session_mod.public_id(child.return_value.info.session_id) == first, "02 is the second task"
        print("keyboard: /task open <number> opens the note with that number  ✓")

        # A different project never sees these tasks.
        with tempfile.TemporaryDirectory() as elsewhere:
            stranger = build(Path(elsewhere))
            async with stranger.run_test(size=(110, 36)) as pilot:
                await pilot.pause()
                assert not stranger.query_one(NoteStrip).has_class("shown")
        print("the strip follows the task you open, speaks both languages, and stays inside its project  ✓")
    print("TUI NOTE STRIP OK — rocky show tasks as notes, click note, go there. amaze!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        _HOME.cleanup()
