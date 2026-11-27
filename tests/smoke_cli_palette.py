"""The CLI keeps to the palette: explicit hex, never an ANSI colour name.

rockycode/palette.py bans named colours ("cyan", "magenta", "green" render
differently in every terminal). The TUI held to that; the CLI did not: typer's
help page came in cyan, yellow and green, Rich's auto-highlighter painted
numbers, paths and quoted text, and a few lines used [cyan] / [magenta] /
[yellow] by name. `--help` is the first thing a new user runs.
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
home = tempfile.mkdtemp(prefix="rocky-palette-")
ENV = {**os.environ, "ROCKYCODE_HOME": home, "PYTHONPATH": str(ROOT), "FORCE_COLOR": "1", "COLUMNS": "100",
       "TERM": "xterm-256color", "COLORTERM": "truecolor"}
ENV.pop("NO_COLOR", None)


def named_colours(text: str) -> list[int]:
    """SGR parameters that pick one of the 16 terminal colours (30–37, 90–97 and
    their backgrounds). Truecolor (38;2;r;g;b) is the palette and is skipped."""
    found = []
    for seq in re.findall(r"\x1b\[([0-9;]*)m", text):
        params = [int(p) for p in seq.split(";") if p]
        i = 0
        while i < len(params):
            p = params[i]
            if p in (38, 48):
                i += 5 if i + 1 < len(params) and params[i + 1] == 2 else 3
                continue
            if 30 <= p <= 37 or 90 <= p <= 97 or 40 <= p <= 47 or 100 <= p <= 107:
                found.append(p)
            i += 1
    return found


assert named_colours("\x1b[1;36mx\x1b[0m\x1b[38;2;169;177;36my\x1b[0m\x1b[38;5;36mz") == [36], "the detector itself"

pages = 0
for args in (["--help"], ["chat", "--help"], ["exec", "--help"], ["goal", "--help"], ["config", "--help"],
             ["bench", "--help"], ["dream", "--help"], ["no-such-command"]):
    out = subprocess.run([sys.executable, "-m", "rockycode.cli", *args], capture_output=True, text=True, env=ENV, cwd=ROOT)
    text = out.stdout + out.stderr
    assert "\x1b[" in text, f"{args}: colour was not forced, the check would prove nothing"
    assert "38;2;" in text, f"{args}: the palette (truecolor) is expected on a help page"
    assert not named_colours(text), (args, sorted(set(named_colours(text))))
    pages += 1
print(f"help: {pages} pages and the usage error use palette hex only, no ANSI colour names  ✓")

# The shared consoles do not auto-highlight (numbers, paths, URLs, quoted text).
from rockycode.cli import _common  # noqa: E402

assert _common.console._highlight is False
loose = [f"{f.relative_to(ROOT)}:{n}" for f in sorted((ROOT / "rockycode").rglob("*.py"))
         for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
         if re.search(r"\bConsole\((?![^)]*highlight=False)", line)]
assert not loose, f"a Console without highlight=False paints ANSI colours: {loose}"
print("consoles: none auto-highlights  ✓")

# No colour by name in any markup the package prints — code or locale files.
NAMED = re.compile(r"\[(?:(?:bold|dim|italic|underline|reverse|not)\s+)*"
                   r"(?:bright_)?(?:black|red|green|yellow|blue|magenta|cyan|white|grey\d*|gray\d*)"
                   r"(?:\s+on\s+\w+)?\]")
named = [f"{f.relative_to(ROOT)}:{n}" for f in sorted([*(ROOT / "rockycode").rglob("*.py"), *(ROOT / "rockycode").rglob("*.toml")])
         for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1) if NAMED.search(line)]
assert not named, named
print("markup: no colour is named anywhere in the package  ✓")

print("CLI PALETTE SMOKE OK — purple all the way down. amaze!")
