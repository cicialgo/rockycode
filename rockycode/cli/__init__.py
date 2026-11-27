"""rockycode CLI — one module per command.

  chat.py · exec_cmd.py · config.py (config, pricing) · dream.py · memory.py · bench.py ·
  goal.py · mcp.py · serve.py — registered below, in the order `--help` lists them.
  _common.py holds what they share. Heavy imports stay inside the command bodies, so
  `rockycode --help` and `rockycode chat` load nothing they don't use.
"""
from __future__ import annotations

import sys

import typer

# Credentials and endpoint come ONLY from ~/.rockycode (.env / keychain) or an
# explicit shell export. Project .env files are deliberately never loaded — a
# repo must not be able to supply a key or redirect where the key is sent
# (project_env_warnings tells the user when one tries).
from rockycode.onboarding import bootstrap_credentials

bootstrap_credentials()

# `rockycode` without args → `rockycode chat`; a leading flag (`rockycode
# --resume …`, `rockycode --yolo`) reaches chat too — nobody should have to
# remember to type "chat" first.
_TOP_LEVEL_FLAGS = {"--help", "-h", "--version", "-V", "--install-completion", "--show-completion"}
if len(sys.argv) == 1:
    sys.argv.append("chat")
elif sys.argv[1].startswith("-") and sys.argv[1] not in _TOP_LEVEL_FLAGS:
    sys.argv.insert(1, "chat")
# Bare `--resume` (no id following) means "open the picker". Typer options
# can't be both valueless and take a value, so rewrite the bare form to a
# sentinel the chat command understands.
for _i, _a in enumerate(sys.argv if len(sys.argv) > 1 and sys.argv[1] == "chat" else []):
    if _a in ("--resume", "-r") and (_i == len(sys.argv) - 1 or sys.argv[_i + 1].startswith("-")):
        sys.argv[_i] = "--resume=__pick__"

# typer paints --help in ANSI cyan, yellow, green and magenta. rocky's palette is
# hex only (rockycode/palette.py), so the help page takes its colours from there.
from typer import rich_utils as _help  # noqa: E402

from rockycode.palette import BLUE, LAVENDER, MUTED, PURPLE, RED, VIOLET  # noqa: E402

_help.STYLE_OPTION = _help.STYLE_SWITCH = _help.STYLE_NEGATIVE_OPTION = _help.STYLE_NEGATIVE_SWITCH = f"bold {LAVENDER}"
_help.STYLE_COMMANDS_TABLE_FIRST_COLUMN = f"bold {LAVENDER}"
_help.STYLE_METAVAR = f"bold {PURPLE}"
_help.STYLE_USAGE = VIOLET
_help.STYLE_OPTION_ENVVAR = MUTED
_help.STYLE_ABORTED = _help.STYLE_DEPRECATED = _help.STYLE_ERRORS_PANEL_BORDER = RED
_help.STYLE_REQUIRED_SHORT = _help.STYLE_REQUIRED_LONG = RED
_help.RICH_HELP = f"Try [{BLUE}]'{{command_path}} {{help_option}}'[/] for help."

app = typer.Typer(
    help="rockycode — a coding agent harness benchmarked on SWE-bench Verified.",
    no_args_is_help=False,
)
from rockycode.cli._common import REPO_ROOT, console  # noqa: E402,F401 — re-exported


def _version_flag(value: bool) -> None:
    if value:
        import rockycode
        print(f"rockycode {rockycode.__version__}")  # plain print: script-friendly
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_flag, is_eager=True,
        help="Print the version and exit."),
) -> None:
    """Force Typer into multi-command mode."""


# ---- commands ------------------------------------------------------------------
from rockycode.cli import bench, chat, config, dream, exec_cmd, goal, mcp, memory, serve  # noqa: E402

app.command()(chat.chat)
app.command("exec")(exec_cmd.exec_cmd)
app.command()(config.config)
app.command()(config.pricing)
app.command()(dream.dream)
app.add_typer(memory.memory_app, name="memory")
app.add_typer(bench.bench_app, name="bench")
app.command()(goal.goal)
app.command("mcp")(mcp.mcp)
app.command()(serve.serve)
