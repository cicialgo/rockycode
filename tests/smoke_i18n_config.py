"""UI locale config, catalogs and global/project feedback; no network needed."""
import asyncio
import os
import tempfile
from pathlib import Path
from string import Formatter
from types import SimpleNamespace
from unittest.mock import Mock, patch

from typer.testing import CliRunner

from rockycode import cli

from rockycode import config as C
from rockycode.i18n import CATALOG, resolve_locale, tr
from rockycode.engine.selfconfig import build_selfconfig_tool


def fields(template):
    return {name for _, name, _, _ in Formatter().parse(template) if name is not None}


assert CATALOG["en"].keys() == CATALOG["zh"].keys(), "catalog key parity"
for key in CATALOG["en"]:
    assert fields(CATALOG["en"][key]) == fields(CATALOG["zh"][key]), key
assert tr("missing.key", "zh") == "missing.key"
assert tr("config.restart", "unknown") == tr("config.restart", "en")
CATALOG["en"]["test.english_only"] = "Hello {name}"
try:
    assert tr("test.english_only", "zh", name="Rocky") == "Hello Rocky"
finally:
    del CATALOG["en"]["test.english_only"]
assert resolve_locale() == "en"
assert resolve_locale("auto", "zh") == "zh"
assert resolve_locale("auto", "en") == "en"
assert resolve_locale("en", "zh") == "en"
assert resolve_locale("zh", "en") == "zh"
assert resolve_locale([], {}) == "en"

old_path = C.GLOBAL_PATH
with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    C.GLOBAL_PATH = root / "global" / "config.toml"
    workdir = root / "project"
    workdir.mkdir()
    try:
        assert C.load(workdir)["ui_language"] == "auto"
        assert C.describe_value("ui_language", workdir)["source"] == "default"
        for key in C._ALLOWED:
            for malformed in ([], {}, 1, None):
                assert C._sanitize({key: malformed}, C.DEFAULTS)[key] == C.DEFAULTS[key]
        assert C.set_value("language", "zh") == ("zh", None)
        cfg = C.load(workdir)
        assert resolve_locale(cfg["ui_language"], cfg["language"]) == "zh"
        assert C.set_value("ui_language", "en") == ("en", None)
        assert C.load(workdir)["language"] == "zh", "UI setting does not alter model language"
        assert C.describe_value("ui_language", workdir)["source"] == "global"
        assert C.set_project_value(workdir, "ui_language", "zh") == ("zh", None)
        details = C.describe_value("ui_language", workdir)
        assert details["source"] == "project" and details["value"] == "zh"
        assert details["global_value"] == "en"
        feedback = C.change_feedback("ui_language", workdir)
        assert "overrides" in feedback and "Restart" in feedback
        assert "项目配置" in C.change_feedback("ui_language", workdir, "zh")
        out = asyncio.run(build_selfconfig_tool(workdir).fn(action="set", key="ui_language", value="en"))
        assert "[ok]" in out and "overrides" in out and "Restart" in out
        assert C.set_value("ui_language", "invalid")[1] == "invalid value for ui_language: 'invalid'. allowed: auto, en, zh"
        assert "可用值" in C.set_value("ui_language", "invalid", locale="zh")[1]
        assert "未知设置" in C.set_value("missing_key", "en", locale="zh")[1]
        assert "正整数" in C.set_value("max_tokens", "many", locale="zh")[1]
        assert "正数" in C.set_value("max_tokens", "-1", locale="zh")[1]
        for global_key in ("model", "vision_models"):
            C.set_project_value(workdir, global_key, "project-model")
            details = C.describe_value(global_key, workdir)
            assert details["source"] == "default" and details["value"] == ""
            assert details["path"] is None
            C.set_value(global_key, "global-model")
            details = C.describe_value(global_key, workdir)
            assert details["source"] == "global" and details["value"] == "global-model"
            assert details["path"] == C.GLOBAL_PATH
            assert C.change_feedback(global_key, workdir) == ""
        (workdir / C.PROJECT_REL).write_text('ui_language = []\nlanguage = {}\npermission = []\n')
        assert C.load(workdir)["ui_language"] == "en"
        assert C.describe_value("ui_language", workdir)["source"] == "global"
        C.set_value("permission", "careful")
        (workdir / C.PROJECT_REL).write_text('permission = "yolo"\n')
        assert C.describe_value("permission", workdir)["source"] == "global"
        assert C.load(workdir)["permission"] == "careful"
        for language, ui_language in (("zh", "en"), ("en", "zh"), ("auto", "auto")):
            engine = SimpleNamespace(finalize_outcome=Mock())
            runtime = SimpleNamespace(engine=engine, notices=[])
            with patch.dict(os.environ, {"ROCKYCODE_API_KEY": "fixture-home"}), \
                 patch("rockycode.config.load", return_value={**C.DEFAULTS, "language": language,
                                                              "ui_language": ui_language}), \
                 patch("rockycode.runtime.build_runtime", return_value=runtime) as build, \
                 patch("rockycode.tui.app.run_app") as ui, patch("rockycode.cli.chat._print_exit_card"):
                result = CliRunner().invoke(cli.app, ["chat", "--model", "fake", "--workdir", str(workdir)])
            assert result.exit_code == 0, (result.output, result.exception)
            assert ui.call_args.kwargs["language"] == language
            assert ui.call_args.kwargs["ui_language"] == ui_language
            assert (build.call_args.kwargs["prompt_name"] == "rocky-builtin-zh") == (language == "zh")
    finally:
        C.GLOBAL_PATH = old_path

print("I18N CONFIG SMOKE OK — locale resolution, catalogs, malformed enums and save feedback")
