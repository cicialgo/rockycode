"""Permission UI follows the configured language without translating mode IDs.

No API or real tool calls: exercise CLI/wrapper forwarding, rendered help and
picker copy, narrow layouts, dismissal, and keyboard/click mode switching.
"""
import asyncio
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(tempfile.mkdtemp(prefix="rocky-permission-language-"))
os.environ["ROCKYCODE_HOME"] = str(ROOT / "home")
os.chdir(ROOT)

from rich.text import Text
from textual.widgets import Static
from typer.testing import CliRunner

from rockycode import cli
from rockycode.config import DEFAULTS
from rockycode.engine.loop import Engine
from rockycode.engine.permission import CYCLE
from rockycode.tui.app import RockyCodeApp, run_app
from rockycode.tui.chatinput import ChatInput
from rockycode.tui.permission import MODE_ROWS, MODE_ROWS_ZH, PermissionPicker


def check_language_forwarding():
    assert tuple(row[0] for row in MODE_ROWS) == tuple(row[0] for row in MODE_ROWS_ZH) == CYCLE
    for language in ("en", "zh", "auto"):
        engine = SimpleNamespace(finalize_outcome=Mock())
        runtime = SimpleNamespace(engine=engine, notices=[])
        with patch.dict(os.environ, {"ROCKYCODE_API_KEY": "fixture-home"}), \
             patch("rockycode.config.load", return_value={**DEFAULTS, "language": language}), \
             patch("rockycode.runtime.build_runtime", return_value=runtime), \
             patch("rockycode.tui.app.run_app") as ui, patch("rockycode.cli.chat._print_exit_card"):
            result = CliRunner().invoke(cli.app, ["chat", "--model", "fake", "--workdir", str(ROOT)])
        assert result.exit_code == 0, (result.output, result.exception)
        assert ui.call_args.kwargs["language"] == language, ui.call_args
        with patch("rockycode.tui.app.RockyCodeApp") as app:
            run_app(engine, language=language)
        assert app.call_args.kwargs["language"] == language, app.call_args
        app.return_value.run.assert_called_once()


def transcript(app):
    return " ".join(str(w.render()) for w in app.query(Static))


async def wait_until(pilot, predicate):
    for _ in range(40):
        await pilot.pause(0.05)
        if predicate():
            return True
    return False


async def check_ui(language):
    zh = language == "zh"
    engine = Engine(model="fake", client=SimpleNamespace(), workdir=ROOT, registry={})
    app = RockyCodeApp(engine, permission="yolo", permission_weakened=True, language=language,
                       evolve="off")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.2)
        assert app._language == ("zh" if zh else "en")
        assert Text.from_markup(app._perm_chip()).plain == "🔓 YOLO"
        note = app._permission_note()
        for expected in (("自动批准工具调用", "静态限制仍然生效", "需要你回答或选择时，仍会询问") if zh else
                         ("automatically approves tool calls", "static restrictions still apply",
                          "questions and choices still ask for your input")):
            assert expected in note, note
        assert ("YOLO 直接在你的机器上运行" if zh else "yolo runs on your machine") in transcript(app)
        assert ("工具审批提示比默认设置更少" if zh else "lowered permission") in transcript(app)
        assert ("点击切换审批模式" if zh else "click to switch approval mode") in str(
            app.query_one("#permchip").tooltip)
        await app._handle_slash("/help")
        help_copy = transcript(app)
        assert ("YOLO 自动批准工具调用" if zh else "YOLO automatically approves tool calls") in help_copy
        assert ("需要你回答或选择时，仍会询问" if zh else
                "Questions and choices still ask for your input") in help_copy
        assert "{permission_help}" not in help_copy

        await pilot.click("#permchip")
        assert await wait_until(pilot, lambda: len(app.query(PermissionPicker)) == 1)
        picker = app.query_one(PermissionPicker)
        assert ("审批模式" if zh else "approval mode") in str(picker.border_title)
        assert ("本次会话" if zh else "this session") in str(picker.border_subtitle)
        for index, expected in enumerate(("写入和命令均先询问", "高风险操作先询问") if zh else
                                         ("every write & command asks first", "risky actions")):
            assert expected in str(app.query_one(f"#perm-mode-{index}", Static).render())
        yolo = str(app.query_one("#perm-mode-2", Static).render())
        assert ("自动批准工具调用" if zh else "automatically approves tool calls") in yolo, yolo
        assert ("当前" if zh else "now") in yolo, yolo
        keys = str(app.query_one("#perm-mode-keys", Static).render())
        assert ("选择" if zh else "choose") in keys and "shift+tab" in keys, keys
        for width, height in ((80, 24), (60, 20)):
            await pilot.resize_terminal(width, height)
            await pilot.pause(0.1)
            assert app.query_one("#prompt").region.bottom <= height, "picker clipped input"
            assert app.query_one("#permchip").region.bottom <= height, "picker hid status"
        await pilot.press("escape")
        assert await wait_until(pilot, lambda: not app.query(PermissionPicker))
        assert app._permission_mode == "yolo" and app.query_one(ChatInput).has_focus

        # /permission and a click resolve the original mode IDs in either language.
        await app._handle_slash("/permission")
        assert await wait_until(pilot, lambda: len(app.query(PermissionPicker)) == 1)
        await pilot.click("#perm-mode-1")
        assert await wait_until(pilot, lambda: app._permission_mode == "ask")
        assert ("询问" if zh else "ask") in app._perm_chip()
        await pilot.press("shift+tab")
        assert await wait_until(pilot, lambda: app._permission_mode == "yolo")
        await pilot.press("shift+tab")
        assert await wait_until(pilot, lambda: app._permission_mode == "careful")
        assert ("谨慎" if zh else "careful") in app._perm_chip()


async def main():
    check_language_forwarding()
    for language in ("en", "zh", "auto"):
        await check_ui(language)
    print("TUI PERMISSION LANGUAGE OK — en/zh, auto fallback, CLI wiring, help, narrow picker, mode IDs")


asyncio.run(main())
