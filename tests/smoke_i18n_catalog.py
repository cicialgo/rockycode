"""The translation catalogs: one TOML file per language, all alike.

What a contributor adding a language relies on:
  - every locale has exactly the English keys, with the same {placeholders};
  - no hex colour inside any string (palette placeholders only);
  - every key the code asks for exists in English (no silent key-as-text);
  - every /help line in the command table exists;
  - a new <code>.toml is discovered, accepted by config, and falls back to
    English for anything it does not translate.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from string import Formatter

os.environ.setdefault("ROCKYCODE_HOME", tempfile.mkdtemp(prefix="rockyhome-"))

from rockycode import i18n
from rockycode.config import _ALLOWED
from rockycode.i18n import CATALOG, DEFAULT, LOCALES, PALETTE, load_locale, resolve_locale, tr
from rockycode.tui import commands

ROOT = Path(__file__).resolve().parents[1] / "rockycode"
HEX = re.compile(r"#[0-9a-fA-F]{6}")


def fields(text: str) -> set[str]:
    return {f for _, f, _, _ in Formatter().parse(text) if f}


# ---- parity across locales ------------------------------------------------------
assert LOCALES[0] == DEFAULT and set(LOCALES) >= {"en", "zh"}, LOCALES
en = CATALOG[DEFAULT]
for code in LOCALES[1:]:
    cat = CATALOG[code]
    assert cat.keys() == en.keys(), (code, sorted(cat.keys() ^ en.keys())[:10])
    for key, text in en.items():
        assert fields(cat[key]) == fields(text), (code, key, fields(cat[key]), fields(text))
        assert cat[key].strip(), (code, key, "empty")
for code, cat in CATALOG.items():
    bad = [k for k, v in cat.items() if HEX.search(v)]
    assert not bad, (code, "hex colours belong to the palette, use {violet} etc.:", bad[:5])
    leaky = [k for k in cat if any(o.startswith(k + ".") for o in cat)]
    assert not leaky, ("a key cannot also be a table prefix:", leaky)
print(f"catalog: {len(LOCALES)} locales × {len(en)} keys, same keys, same placeholders, no hex  ✓")

# ---- every key the code uses exists ----------------------------------------------
call = re.compile(r"(?:_t|tr)\(\s*(['\"])([a-z_][a-z_.0-9]*)\1|\bkey=(['\"])([a-z_][a-z_.0-9]*)\3")
used: dict[str, str] = {}
for path in ROOT.rglob("*.py"):
    for m in call.finditer(path.read_text(encoding="utf-8")):
        key = m.group(2) or m.group(4)
        if key.endswith((".", "_")):
            continue  # a prefix completed at runtime ("permission.risk." + tier)
        used.setdefault(key, str(path.relative_to(ROOT)))
missing = {k: where for k, where in used.items() if k not in en}
assert not missing, missing
for c in commands.COMMANDS:
    assert c.help is None or c.help in en, c
assert "help.title" in en and "help.shell" in en
print(f"code: {len(used)} literal keys all present in en; every /help line exists  ✓")

# ---- the desktop host sends keys, and every one of them exists ------------------
# apps/desktop is the private tree (absent on the public export): its Python host
# raises HostError("diagnostic.…") / stores error_key and limitation keys — all
# `desktop.*` keys in the same TOML files the desktop catalog is generated from.
DESKTOP_HOST = ROOT.parent / "apps" / "desktop" / "runtime" / "rockycode_desktop"
if DESKTOP_HOST.is_dir():
    host_key = re.compile(r"""['"]((?:diagnostic|race)\.[A-Za-z_.0-9]+)['"]""")
    host_used: dict[str, str] = {}
    for path in DESKTOP_HOST.glob("*.py"):
        for m in host_key.finditer(path.read_text(encoding="utf-8")):
            host_used.setdefault(m.group(1), path.name)
    assert len(host_used) > 40, host_used
    missing = {k: where for k, where in host_used.items() if "desktop." + k not in en}
    assert not missing, missing
    print(f"desktop host: {len(host_used)} message keys all present in en  ✓")

# ---- capability rows name their detail by key, and every key exists -------------
# (A wrong key reaches every client as its own name: the desktop showed
# "capability.mcp_none" for a user with no MCP server.)
cap_keys = set(re.findall(r'"(capability[a-z_]*\.[a-z_]+)"', (ROOT / "runtime.py").read_text(encoding="utf-8")))
assert len(cap_keys) >= 8 and not [k for k in cap_keys if k not in en], sorted(k for k in cap_keys if k not in en)
print(f"runtime: {len(cap_keys)} capability detail keys all present in en  ✓")

# ---- palette placeholders are filled, explicit params win ----------------------
assert tr("app.hints_bar", "en").startswith("[" + PALETTE["muted"] + "]"), tr("app.hints_bar", "en")[:20]
assert "{violet}" not in tr("help.title", "zh")
assert tr("permission.keys", "en", colour="#000000").count("#000000") >= 1

# ---- adding a language is adding a file -----------------------------------------
tmp = Path(tempfile.mkdtemp(prefix="rockylocale-")) / "xx.toml"
tmp.write_text("[help]\ntitle = 'Befehle {violet}'\n", encoding="utf-8")
xx = load_locale(tmp)
assert xx == {"help.title": "Befehle {violet}"}, xx
CATALOG["xx"] = xx
try:
    assert tr("help.title", "xx") == "Befehle " + PALETTE["violet"]
    assert tr("help.plan", "xx") == tr("help.plan", "en"), "untranslated keys fall back to English"
    assert resolve_locale("xx") == "xx" and resolve_locale("auto", "xx") == "xx"
finally:
    del CATALOG["xx"]
assert resolve_locale("yy") == "en" and resolve_locale("auto", "yy") == "en"
assert _ALLOWED["ui_language"] == {"auto", *LOCALES}, _ALLOWED["ui_language"]
assert i18n.LOCALES_DIR.is_dir()
print("locale file: discovered, accepted by config, falls back to English  ✓")

# ---- the desktop export is generated from the same files -------------------------
import subprocess
import sys
gen = ROOT.parent / "scripts" / "i18n_desktop.py"
target = ROOT.parent / "apps" / "desktop" / "src" / "shared" / "catalog.ts"
if target.exists():
    r = subprocess.run([sys.executable, str(gen), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr.strip() or "desktop catalog export is stale"
    desk = [k for k in en if k.startswith("desktop.")]
    assert "desktop.language.name" in en and "desktop.language.tag" in en
    print(f"desktop: {len(desk)} desktop.* keys exported, catalog.ts up to date  ✓")
else:
    print("desktop: apps/desktop not present here, export check skipped")
print("I18N CATALOG SMOKE OK")
