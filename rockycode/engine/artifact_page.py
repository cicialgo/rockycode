"""rocky's own page for an HTML fragment: one stylesheet, three looks, light and dark.

The model writes plain HTML — a heading, paragraphs, a table, a drawing — and
chooses no colours and no fonts. This page supplies them, so every artifact
reads the same way and none depends on a model's taste that day. A full HTML
document never gets it: that is the user's own design, kept as written.

How it is built:
  - seven colours per look (TOKENS), one set for light and one for dark; the
    dark set follows the reader's system through `prefers-color-scheme`;
  - every rule is `:where(…)`, so it has zero specificity and any CSS the
    fragment brings wins without a fight;
  - system fonts only: an artifact opens from a file with no network;
  - drawings follow the look through four classes (box, hot, flow, quiet), so
    an SVG never hard-codes a colour that turns unreadable in the dark.

The built-in `artifact-design` skill teaches the model how to write for it.
"""
from __future__ import annotations

TOKENS = ("paper", "ink", "muted", "line", "wash", "accent", "accent-wash")

# paper: the page · ink: text · muted: captions and labels · line: hairlines ·
# wash: code and quiet boxes · accent: the one colour · accent-wash: a note's fill.
# Text pairs are at least 4.5:1 in every set (tests/smoke_artifact_page.py).
LOOKS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {  # name → (light, dark)
    "ink": (("#f7f8fa", "#1c2230", "#5c667a", "#dfe3ea", "#eef1f5", "#2456a6", "#e4ecf8"),
            ("#14171c", "#dde2ea", "#98a2b3", "#2a303a", "#1c2027", "#86aef2", "#1b2740")),
    "amber": (("#f8f8f6", "#23211d", "#6b665c", "#e3e0d8", "#f0eee8", "#8a5a12", "#f5ead6"),
              ("#171614", "#e7e3da", "#a39d90", "#2e2c28", "#1f1e1b", "#d8b27d", "#2e2618")),
    # rocky's own purple: the terminal's palette on a page.
    "lilac": (("#f6f4fb", "#2a2a38", "#666680", "#ddd4ef", "#ece8f7", "#6a4ca3", "#e9e1f7"),
              ("#16161e", "#c0caf5", "#8a8fb0", "#2a2a3a", "#1e1e28", "#bb9af7", "#272238")),
}
DEFAULT_LOOK = "ink"
NO_LOOK = "none"                  # typography only, no colours of rocky's: bring your own CSS
ALIASES = {"rocky": "lilac"}      # the purple preset's first name; saved artifacts still carry it
THEMES = (*LOOKS, NO_LOOK)
LAYOUTS = {"document": "40em", "wide": "72em"}


def resolve_theme(theme: str) -> str:
    """A theme name as it is stored: a look, or `none`. ValueError for anything else."""
    theme = ALIASES.get(theme, theme)
    if theme not in THEMES:
        raise ValueError(f"theme must be one of {', '.join(THEMES)}")
    return theme


def _tokens(values: tuple[str, ...]) -> str:
    return " ".join(f"--{name}:{value};" for name, value in zip(TOKENS, values))


_PAGE = """
:where(html) { background:var(--paper); color:var(--ink); color-scheme:light dark; }
:where(body) {
  font:17px/1.7 ui-sans-serif,system-ui,-apple-system,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC",sans-serif;
  max-width:var(--measure); margin:0 auto; padding:clamp(1.25rem,4vw,3.5rem) clamp(1rem,4vw,2rem) 5rem;
  text-align:left; overflow-wrap:break-word; -webkit-text-size-adjust:100%;
}
:where(h1,h2) { font-family:ui-rounded,ui-sans-serif,system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }
:where(h1,h2,h3) { line-height:1.25; font-weight:700; text-wrap:balance; }
:where(h1) { font-size:2rem; margin:0 0 .9rem; letter-spacing:-.01em; }
:where(h2) { font-size:1.35rem; margin:2.6rem 0 .7rem; padding-top:1.5rem; border-top:1px solid var(--line); }
:where(h3) { font-size:1.05rem; margin:1.8rem 0 .4rem; }
:where(p,ul,ol,table,pre,figure,blockquote,details) { margin:0 0 1.1rem; }
:where(h1 + p) { font-size:1.2rem; line-height:1.55; padding:.1rem 0 .1rem 1rem; border-left:4px solid var(--accent); margin-bottom:1.6rem; }
:where(a) { color:var(--accent); text-underline-offset:.18em; }
:where(strong) { font-weight:650; }
:where(small,figcaption,.muted) { color:var(--muted); font-size:.875rem; }
:where(ul,ol) { padding-left:1.3rem; }
:where(li) { margin:.3rem 0; }
:where(li)::marker { color:var(--accent); }
:where(hr) { border:0; border-top:1px solid var(--line); margin:2.2rem 0; }
:where(code,kbd,pre) { font-family:ui-monospace,"SF Mono","Cascadia Code","JetBrains Mono",Menlo,Consolas,monospace; font-size:.88em; }
:where(code) { background:var(--wash); padding:.12em .38em; border-radius:5px; }
:where(pre) { background:var(--wash); padding:1rem 1.15rem; border-radius:10px; overflow-x:auto; line-height:1.55; }
:where(pre code) { background:none; padding:0; font-size:1em; }
:where(kbd) { border:1px solid var(--line); border-bottom-width:2px; border-radius:5px; padding:.05em .4em; }
:where(mark) { background:var(--accent-wash); color:inherit; padding:.05em .25em; border-radius:4px; }
:where(blockquote) { background:var(--accent-wash); border-radius:10px; padding:.85rem 1.1rem; }
:where(blockquote > :last-child, .card > :last-child, details > :last-child) { margin-bottom:0; }
:where(table) { border-collapse:collapse; width:100%; font-size:.95rem; }
:where(th) { text-align:left; font-weight:600; color:var(--muted); font-size:.85rem; border-bottom:1px solid var(--ink); padding:.5rem .75rem .5rem 0; }
:where(td) { border-bottom:1px solid var(--line); padding:.6rem .75rem .6rem 0; vertical-align:top; }
:where(figure) { margin-left:0; margin-right:0; }
:where(img,svg,video) { max-width:100%; height:auto; }
:where(details) { border:1px solid var(--line); border-radius:10px; padding:.7rem 1.1rem; }
:where(summary) { cursor:pointer; font-weight:600; }
:where(details[open] > summary) { margin-bottom:.6rem; }
:where(.grid) { display:grid; gap:.9rem; margin:0 0 1.1rem; grid-template-columns:repeat(auto-fit,minmax(13rem,1fr)); }
:where(.card) { border:1px solid var(--line); border-radius:12px; padding:1rem 1.1rem; }
:where(.card > :first-child) { margin-top:0; }
:where(.tag) { display:inline-block; font-size:.8rem; padding:.05rem .55rem; border-radius:99px; background:var(--wash); color:var(--muted); }
:where(svg) { color:var(--ink); }
:where(svg text:not([fill])) { fill:var(--ink); font:14px ui-sans-serif,system-ui,"PingFang SC","Microsoft YaHei",sans-serif; }
:where(svg .box) { fill:var(--wash); stroke:var(--line); stroke-width:1.2; }
:where(svg .hot) { fill:var(--accent-wash); stroke:var(--accent); stroke-width:1.6; }
:where(svg .flow) { stroke:var(--muted); stroke-width:1.4; fill:none; }
:where(svg .tip) { fill:var(--muted); stroke:none; }
:where(svg text.quiet) { fill:var(--muted); font-size:12px; }
:where(html[data-layout="wide"]) :where(h1,h2,h3,p,ul,ol,blockquote,details) { max-width:44rem; }
@media print { :where(html) { background:#fff; } :where(body) { max-width:none; } }
"""


def page_css(theme: str, layout: str = "document") -> str:
    """The stylesheet for one look (THEMES minus `none`), light and dark."""
    light, dark = LOOKS[resolve_theme(theme)]
    return (f":root {{ --measure:{LAYOUTS[layout]}; {_tokens(light)} }}\n"
            f"@media (prefers-color-scheme: dark) {{ :root {{ {_tokens(dark)} }} }}" + _PAGE)
