"""Rocky-themed terminal output. amaze amaze amaze!

All colors come from rockycode.palette — never ANSI named colors.
"""
from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from rockycode.palette import AMBER, LAVENDER, MUTED, PURPLE, RED, VIOLET

ROCKY_TAGLINE = "i learn traditional physics. i no know e=mc^2 yet. but we fix bug. amaze!"


def show_banner(console: Console) -> None:
    title = Text("ROCKYCODE", style=f"bold {VIOLET}")
    subtitle = Text("a coding agent harness, benchmarked", style=MUTED)
    quote = Text(ROCKY_TAGLINE, style=f"italic {LAVENDER}")
    body = Text.assemble(title, "\n", subtitle, "\n\n", quote)
    console.print(Panel(body, border_style=PURPLE, expand=False, padding=(1, 4)))


def amaze(console: Console, message: str = "amaze!") -> None:
    console.print(f"[bold {VIOLET}]✦[/] [italic {VIOLET}]{message}[/]")


def confused(console: Console, message: str = "i no know! i learn!") -> None:
    console.print(f"[bold {AMBER}]?[/] [italic {AMBER}]{message}[/]")


def fail(console: Console, message: str = "we no good. we try again.") -> None:
    console.print(f"[bold {RED}]✗[/] [italic {RED}]{message}[/]")


def info(console: Console, message: str) -> None:
    console.print(f"[{MUTED}]·[/] {message}")
