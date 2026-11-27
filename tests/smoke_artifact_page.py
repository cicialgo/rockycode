"""rocky's own page for an artifact: the model writes plain HTML, the page brings the look.

A fragment gets one stylesheet with the user's look (ink by default, never
purple unless they pick it), light and dark. A full document is left alone.
The stylesheet has zero specificity, so CSS a fragment brings wins. Every text
colour is readable on its background in every look. The built-in
`artifact-design` skill names only parts the stylesheet really has.

Hermetic: a throwaway home, real files in a temp project, no browser, no model.
"""
import asyncio
import os
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace

home = Path(tempfile.mkdtemp(prefix="rocky-artifactpage-home-"))
os.environ["ROCKYCODE_HOME"] = str(home)  # before the import: the store path is fixed there

from rockycode import config as C  # noqa: E402
from rockycode.engine import artifact_page as page  # noqa: E402
from rockycode.engine.artifact import _artifact_html, build_artifact_tools, default_theme, page_fragment  # noqa: E402
from rockycode.engine.skills import discover_skills  # noqa: E402

assert C.GLOBAL_PATH == home / "config.toml", C.GLOBAL_PATH
FRAGMENT = "<h1>Report</h1><p>The answer.</p><h2>Why</h2><table><tr><th>a</th></tr><tr><td>b</td></tr></table>"
FULL = "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><style>body{background:navy}</style></head><body>mine</body></html>"


def style_of(doc: str) -> str:
    return re.search(r"(?s)<style>(.*?)</style>", doc).group(1)


# ── the default look is ink, in light and in dark ───────────────────────────
doc = _artifact_html("Report", FRAGMENT)
css = style_of(doc)
light, dark = page.LOOKS["ink"]
assert page.DEFAULT_LOOK == "ink" and all(f"--{t}:{v};" in css for t, v in zip(page.TOKENS, light))
assert "@media (prefers-color-scheme: dark)" in css and all(v in css.split("@media (prefers-color-scheme: dark)")[1]
                                                            for v in dark)
assert not any(purple in doc for purple in page.LOOKS["lilac"][0]), "purple is a choice, not the default"
assert doc.count("<h1>") == 1 and FRAGMENT in doc and '<html data-layout="document" data-rocky-page="ink">' in doc
assert "color-scheme:light dark" in css and "--measure:40em" in css
assert "--measure:72em" in style_of(_artifact_html("Report", FRAGMENT, "ink", "wide"))
assert "<h1>Plan &amp; notes</h1>" in _artifact_html("Plan & notes", "<p>x</p>"), "no <h1>: the title is the heading"
assert "http" not in css and "@import" not in css and "url(" not in css, "the page loads nothing: system fonts only"
print("default: a fragment gets the ink look, light and dark, and loads nothing  ✓")

# ── each look; a full document and theme none get no colours of rocky's ─────
for name, (light, dark) in page.LOOKS.items():
    css = style_of(_artifact_html("X", FRAGMENT, name))
    assert all(value in css for value in (*light, *dark)), name
assert style_of(_artifact_html("X", FRAGMENT, "rocky")) == style_of(_artifact_html("X", FRAGMENT, "lilac")), \
    "an artifact saved with the first purple preset keeps a purple page"
assert _artifact_html("X", FULL) == FULL and _artifact_html("X", FULL, "amber", "wide") == FULL
bare = _artifact_html("X", FRAGMENT, "none")
assert "#" not in style_of(bare) and "var(--" not in style_of(bare), "none: type only, no colour and no token"
for wrong in ("purple", "", "INK"):
    try:
        _artifact_html("X", FRAGMENT, wrong)
    except ValueError as error:
        assert "ink, amber, lilac, none" in str(error)
    else:
        raise AssertionError(f"theme {wrong!r} must be refused")
print("looks: ink, amber, lilac; `rocky` still means lilac; a full document is kept as written  ✓")


# ── every text colour is readable on what it sits on, in every look ─────────
def luminance(colour: str) -> float:
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(int(colour[i:i + 2], 16) / 255) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(one: str, other: str) -> float:
    hi, lo = sorted((luminance(one), luminance(other)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


weakest = 99.0
for name, modes in page.LOOKS.items():
    for mode, values in zip(("light", "dark"), modes):
        c = dict(zip(page.TOKENS, values))
        for text, ground in (("ink", "paper"), ("muted", "paper"), ("accent", "paper"), ("ink", "wash"),
                             ("muted", "wash"), ("ink", "accent-wash"), ("accent", "accent-wash")):
            ratio = contrast(c[text], c[ground])
            weakest = min(weakest, ratio)
            assert ratio >= 4.5, f"{name} {mode}: {text} on {ground} is {ratio:.1f}:1"
print(f"contrast: every text pair in 3 looks x 2 modes is at least 4.5:1 (weakest {weakest:.1f}:1)  ✓")

# ── zero specificity: what a fragment brings always wins ────────────────────
rules = re.sub(r"(?s)@media[^{]*\{(.*?\})\s*\}", r"\1", page.page_css("ink"))
selectors = [s.strip() for s in re.findall(r"(?m)^([^{}@\n][^{}]*)\{", rules)]
assert len(selectors) > 30, selectors
for selector in selectors:
    rest = re.sub(r":where\((?:[^()]|\([^()]*\))*\)", "", selector)
    assert selector == ":root" or re.fullmatch(r"[\s>+~,]*(::marker)?", rest), f"not zero specificity: {selector}"
assert not re.search(r"!important", page.page_css("ink"))
own = "<style>h1{color:rgb(1,2,3)}</style><h1>Mine</h1><p>x</p>"
doc = _artifact_html("X", own)
assert doc.index("--paper") < doc.index("rgb(1,2,3)"), "and it comes after the page in the source"
print(f"cascade: all {len(selectors)} rules are :where(), nothing is !important; a fragment's own CSS wins  ✓")

# ── a drawing follows the look; a colour the author set is left alone ───────
css = page.page_css("ink")
assert ":where(svg text:not([fill]))" in css, "a text with its own fill keeps it (a CSS rule would beat the attribute)"
for part in ("svg .box", "svg .hot", "svg .flow", "svg .tip", "svg text.quiet"):
    assert f":where({part})" in css, part
print("drawings: box, hot, flow, tip, quiet take the look's colours; an authored fill is not overridden  ✓")


# ── the user's default, a project's, and what a revision keeps ──────────────
async def tool_flow():
    project = Path(tempfile.mkdtemp(prefix="rocky-artifactpage-"))
    host = SimpleNamespace(trajectory=SimpleNamespace(session_id="s1"), artifact_auto_open=False)
    made = build_artifact_tools(workdir=project, engine=host)
    create, inspect = made["create_artifact"], made["inspect_artifact"]
    assert create.schema["function"]["parameters"]["properties"]["theme"]["enum"] == ["ink", "amber", "lilac", "none"]
    assert "artifact-design" in create.schema["function"]["description"]
    registry = host.artifact_registry

    def latest():
        return registry.records()[0]

    assert default_theme(project) == "ink"
    assert "[ok]" in await create.fn("First", FRAGMENT)
    first = latest()
    assert first.theme == "ink" and page.LOOKS["ink"][0][0] in first.path.read_text()

    _value, error = C.set_value("artifact_theme", "lilac")  # the user's own choice: /config artifact_theme lilac
    assert error is None and default_theme(project) == "lilac"
    assert "[ok]" in await create.fn("Second", FRAGMENT)
    assert latest().theme == "lilac" and page.LOOKS["lilac"][0][0] in latest().path.read_text()
    # a revision keeps the look its artifact has, whatever the default is now
    assert "[ok]" in await create.fn("First", FRAGMENT + "<p>more</p>", first.artifact_id, 1)
    revised = registry.get(first.artifact_id)
    assert (revised.revision, revised.theme) == (2, "ink") and page.LOOKS["lilac"][0][0] not in revised.path.read_text()
    # asked for by name, for this artifact only
    assert "[ok]" in await create.fn("Third", FRAGMENT, theme="amber", layout="wide")
    assert (latest().theme, latest().layout) == ("amber", "wide")
    assert "[ok]" in await create.fn("Old preset", FRAGMENT, theme="rocky")
    assert latest().theme == "lilac", "the old name is stored under the new one"
    assert "[error]" in await create.fn("Bad", FRAGMENT, theme="purple")
    # Editing: the model reads the fragment it wrote, not the page around it, and what it sends
    # back goes on the page again. The stylesheet is never re-sent, and the look is kept.
    import json
    read = json.loads(await inspect.fn(first.artifact_id))
    assert read["fragment"] is True and read["html"] == FRAGMENT + "<p>more</p>" and "--paper" not in read["html"]
    assert "send a fragment back" in read["read_hint"]
    assert "[ok]" in await create.fn("First", read["html"].replace("more", "even more"), first.artifact_id, 2)
    third = registry.get(first.artifact_id)
    saved = third.path.read_text()
    assert (third.revision, third.theme) == (3, "ink") and saved.count("--paper:") == 2 and saved.count("<h1>") == 1
    assert page_fragment(saved) == FRAGMENT + "<p>even more</p>"
    untitled = "<p>no heading here</p>"
    assert "[ok]" in await create.fn("Titled <by> rocky", untitled)
    read = json.loads(await inspect.fn(latest().artifact_id))
    assert read["html"] == "<h1>Titled &lt;by&gt; rocky</h1>" + untitled, "the heading rocky added comes back with it"
    assert "[ok]" in await create.fn("Their own", FULL)
    read = json.loads(await inspect.fn(latest().artifact_id))
    assert read["fragment"] is False and read["html"] == FULL, "a full document is read back whole"
    tricky = "<style>.x{}</style></head><body><p>a fragment that quotes page markup</p></body></html><p>tail</p>"
    assert page_fragment(_artifact_html("X", "<h1>t</h1><pre>" + tricky.replace("<", "&lt;") + "</pre>" )) is not None
    for other in (FULL, "<p>x</p>", "", '<!DOCTYPE html>\n<html data-layout="document"><head></head><body>x</body></html>'):
        assert page_fragment(other) is None
    # a project may have its own; a bad value in a file falls back instead of breaking a save
    (project / ".rockycode").mkdir(exist_ok=True)
    (project / ".rockycode" / "config.toml").write_text('artifact_theme = "amber"\n')
    assert default_theme(project) == "amber" and default_theme(Path(tempfile.mkdtemp())) == "lilac"
    (project / ".rockycode" / "config.toml").write_text('artifact_theme = "chartreuse"\n')
    assert default_theme(project) == "lilac"
    _value, error = C.set_value("artifact_theme", "chartreuse")
    assert error and "ink" in error, "an unknown look is refused with the list"
    C.set_value("artifact_theme", "ink")


asyncio.run(tool_flow())
print("default look: the user's setting for new artifacts; a revision keeps its own; a project may set one  ✓")

# ── the skill ships, and names only what the page has ───────────────────────
skills = {s.name: s for s in discover_skills(Path(tempfile.mkdtemp()), home=None)}
assert "artifact-design" in skills and skills["artifact-design"].source == "built-in", list(skills)
text = skills["artifact-design"].path.read_text()
css = page.page_css("ink")
for token in re.findall(r"var\((--[a-z-]+)\)", text):
    assert token[2:] in page.TOKENS, f"the skill names a colour the page does not have: {token}"
assert {t for t in page.TOKENS} == {t[2:] for t in re.findall(r"var\((--[a-z-]+)\)", text)}, "and it names all seven"
for cls in re.findall(r'class="([a-z ]+)"', text):
    for name in cls.split():
        assert re.search(rf"\.{name}\b", css), f"the skill uses class {name!r}, which the page does not style"
for look in page.THEMES:
    assert look in text, look
assert "#" not in re.sub(r"(?m)^#+ .*$|url\(#tip\)|fill=\"#…\"|stroke=\"#…\"", "", text), \
    "the skill itself shows no hard-coded colour"
print("skill: artifact-design is built in, and every colour and class it teaches exists on the page  ✓")

print("ARTIFACT PAGE SMOKE OK — model write word, page bring colour. amaze!")
