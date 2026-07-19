"""rockycode's one palette — shared by the CLI (Rich) and the TUI (Textual).

Explicit hex only. ANSI named colors ("magenta", "cyan") render differently
in every terminal — neon pink, electric blue — and are banned in this repo.
This file is the single source of truth for both surfaces; if a color isn't
here, don't use it.
"""

VIOLET = "#bb9af7"    # brand: titles, headings, "amaze!"
PURPLE = "#9d7cd8"    # structure: borders, progress bars, tool marks
LAVENDER = "#a9b1d6"  # secondary text, quotes
BLUE = "#7aa2f7"      # highlights inside text: paths, dataset names, links
AMBER = "#d8b27d"     # warnings / "i no know"
RED = "#e06c75"       # errors only
MUTED = "#787c99"     # de-emphasized text (works where terminal `dim` doesn't)
