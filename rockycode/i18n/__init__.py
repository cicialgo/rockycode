"""UI text, one file per language: rockycode/i18n/locales/<code>.toml.

Adding a language is adding a file: copy en.toml to <code>.toml, translate the
values, keep every {placeholder}, run tests/smoke_i18n_catalog.py. The code
discovers it (`LOCALES`), `rockycode config ui_language <code>` accepts it,
and missing strings fall back to English. No framework, no runtime dependency.

Only UI-owned text lives here: chrome, hints, help, command feedback, pickers,
approval controls, status text, runtime capability details. User messages,
model output, tool output, file names, model ids, slash-command syntax and the
agent prompts are never translation keys.

Colour placeholders ({violet} {purple} {lavender} {blue} {amber} {red} {muted})
are filled from rockycode/palette.py by `tr`, so the catalogs never carry a hex
value and a palette change touches one file. A caller's explicit parameter of
the same name wins.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from rockycode import palette

LOCALES_DIR = Path(__file__).parent / "locales"
DEFAULT = "en"
PALETTE = {name.lower(): getattr(palette, name)
           for name in ("VIOLET", "PURPLE", "LAVENDER", "BLUE", "AMBER", "RED", "MUTED")}


def _flatten(table: dict, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in table.items():
        if isinstance(value, dict):
            out.update(_flatten(value, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = str(value)
    return out


def load_locale(path: Path) -> dict[str, str]:
    """One locale file → a flat {"section.name": text} catalog."""
    return _flatten(tomllib.loads(Path(path).read_text(encoding="utf-8")))


CATALOG: dict[str, dict[str, str]] = {
    p.stem: load_locale(p) for p in sorted(LOCALES_DIR.glob("*.toml"))
}
LOCALES: tuple[str, ...] = (DEFAULT, *sorted(code for code in CATALOG if code != DEFAULT))


def resolve_locale(ui_language: str = "auto", language: str = "auto") -> str:
    """An explicit, known `ui_language` wins. `auto` follows the reply language
    where a catalog exists for it (zh), else English — no OS locale sniffing."""
    if isinstance(ui_language, str) and ui_language != "auto" and ui_language in CATALOG:
        return ui_language
    if isinstance(language, str) and language != "auto" and language in CATALOG:
        return language
    return DEFAULT


def tr(key: str, locale: str = DEFAULT, **params: object) -> str:
    """Translate a UI key: the locale's text, else English, else the key itself.

    Values are formatted only here; callers keep dynamic content out of keys.
    A missing or wrong parameter raises, so a translation mistake stays visible.
    """
    messages = CATALOG.get(locale, {}) if isinstance(locale, str) else {}
    template = messages.get(key, CATALOG[DEFAULT].get(key, key))
    return template.format(**{**PALETTE, **params})
