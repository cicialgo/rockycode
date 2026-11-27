"""Command feedback uses UI locale and accurately reports project overrides."""
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rockycode import config
from rockycode.i18n import tr
from rockycode.i18n import CATALOG
from rockycode.tui.app import RockyCodeApp


class CommandHarness:
    _handle_slash = RockyCodeApp._handle_slash
    _t = RockyCodeApp._t

    def __init__(self, workdir, locale):
        self._language = locale
        self.engine = SimpleNamespace(workdir=workdir, skills=[], memory_store=None)
        self.lines = []

    async def _add(self, widget):
        self.lines.append(str(widget.render()))


async def main():
    with tempfile.TemporaryDirectory() as root:
        root = Path(root)
        project = root / "project"
        project.mkdir()
        (project / ".rockycode").mkdir()
        (project / ".rockycode/config.toml").write_text(
            'ui_language = "zh"\ncontext_window = 1234\n', encoding="utf-8"
        )
        with patch.object(config, "GLOBAL_PATH", root / "global.toml"):
            app = CommandHarness(project, "zh")
            await app._handle_slash("/config ui_language en")
            assert "项目配置" in app.lines[-1] and "重启" in app.lines[-1]
            assert "已立即生效" not in app.lines[-1]
            await app._handle_slash("/config context_window 5678")
            assert "1234" in app.lines[-1] and "项目配置" in app.lines[-1]
            # There is deliberately no engine.set_limit: a masked save must not call it.
            await app._handle_slash("/config")
            assert "项目覆盖" in app.lines[-1] and str(project) in app.lines[-1]
            await app._handle_slash("/skills")
            assert "未找到技能" in app.lines[-1]
            await app._handle_slash("/memory")
            assert "记忆已关闭" in app.lines[-1]
            await app._handle_slash("/typo")
            assert "不认识命令" in app.lines[-1] and "/typo" in app.lines[-1]
            app._language = "en"
            await app._handle_slash("/skills")
            assert "no skills found" in app.lines[-1]

    # Every translated command accepts the identical fields, including literal {path}.
    from string import Formatter
    for key, english in CATALOG["en"].items():
        if not key.startswith("commands."):
            continue  # this smoke owns the command feedback; the catalog smoke covers the rest
        fields = {field for _, field, _, _ in Formatter().parse(english) if field}
        chinese_fields = {field for _, field, _, _ in Formatter().parse(CATALOG["zh"][key]) if field}
        assert fields == chinese_fields, key
        for locale in ("en", "zh"):
            tr(key, locale, **{field: 1 for field in fields})
    print("command locales, placeholders, and project override feedback: ok")


asyncio.run(main())
