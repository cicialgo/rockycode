"""Markdown → terminal-friendly markdown, applied just before Textual's
Markdown widget parses it (ported from a research branch).

Two failure modes this fixes:
- Textual intentionally disables auto-links inside fenced code blocks, and
  VS Code's terminal link detector only sees one visual row at a time — a long
  local path that soft-wraps LOOKS complete but clicking captures only the
  first row. So absolute-path link targets are rewritten to real file:// URIs
  (clickable anywhere), and each code fence gets a compact "path links:" line
  appended after it for the paths trapped inside.
- Long tree/path lines inside fences don't soft-wrap (code blocks clip), so
  tree output loses its tail. Path-looking lines are wrapped at cell width
  (CJK-aware), breaking at / _ - with a continuation indent; code-looking
  lines are left alone (wrapping code would corrupt it).

Pure functions, no Textual imports — unit-tested in tests/smoke_mdterm.py.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from markdown_it import MarkdownIt
from rich.cells import cell_len

# Absolute paths under the roots real user files live in (macOS + Linux +
# common containers) — anchored so URL fragments like /v1/messages don't match.
_ROOT = r"/(?:Users|home|root|tmp|private|var|opt|srv|mnt|etc|workspace)/"
ABS_PATH_RE = re.compile(r"(?<![\w:])(" + _ROOT + r"[^\s`<>\]\)]+)")
ABS_LINK_TARGET_RE = re.compile(r"\]\((" + _ROOT + r"[^)\s]+)\)")
ABS_ANGLE_LINK_TARGET_RE = re.compile(r"\]\(<(" + _ROOT + r"[^>]+)>\)")
_ROOT_RE = re.compile(_ROOT)
TREE_OR_PATH_HINT_RE = re.compile(r"(" + _ROOT + r"|[├└│┬┴─]|←|✅|\.md\b|\.txt\b|\.pdf\b|/)")
CODELIKE_PUNCT_RE = re.compile(r"[{};=()]")
# `loop.py:128` / `loop.py:128:5` — the label keeps it, the URI can't.
_LINE_SUFFIX_RE = re.compile(r":\d+(?::\d+)?$")


# Click policy: clicking is for CHECKING things — the
# paper, the docs, the link — never for RUNNING things. Reading files open
# with the system default; code/shell/anything else shows a dim note instead.
READING_SUFFIXES = {
    ".md", ".markdown", ".rst", ".txt", ".pdf", ".html", ".htm",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
}


def link_click_action(href: str) -> tuple[str, str]:
    """Decide what a clicked link may do.

    Returns ("browser", href) for the web, ("open", file_uri) for an existing
    reading file, ("blocked", path) for code or anything that could execute,
    ("missing", path) for a target that moved or was renamed since the reply.

    HARD RULE (enforced here, not by the model): "open" is returned only for
    a regular file whose RESOLVED target has a reading suffix and no exec bit.
    Symlinks are judged by what they point at — `innocent.md → evil.command`
    is blocked, because macOS `open` follows the link and would RUN a
    .command/.app/unix-executable target. Nothing a click opens can execute.
    """
    parts = urlsplit(href)
    scheme = parts.scheme.lower()
    if scheme in ("http", "https"):
        return ("browser", href)
    if scheme not in ("", "file"):
        return ("blocked", href)
    path = url2pathname(parts.path)
    if not path.startswith("/"):
        return ("blocked", path)
    real = Path(path).resolve()
    if real.suffix.lower() not in READING_SUFFIXES:
        return ("blocked", path)
    if not real.exists():
        return ("missing", path)
    if not real.is_file() or os.access(real, os.X_OK):
        return ("blocked", path)
    return ("open", real.as_uri())


def rocky_markdown_parser() -> MarkdownIt:
    """markdown-it tuned for the transcript: file:// hrefs allowed (the stock
    security filter drops them and the whole link renders as raw text), fuzzy
    linkify off (`loop.py` is a file, not a Paraguayan website — explicit
    https:// URLs still autolink)."""
    md = MarkdownIt("gfm-like")
    stock_validate = md.validateLink
    md.validateLink = lambda url: url.startswith("file://") or stock_validate(url)
    md.linkify.set({"fuzzy_link": False})
    return md


def _file_uri(path: str) -> str:
    try:
        return Path(path).as_uri()
    except ValueError:
        return path


def _rewrite_link_targets(line: str) -> str:
    # `:128` must come off before as_uri() — quoted to %3A128 it points at a
    # file that doesn't exist. The label lives in [...] and keeps the suffix.
    def uri(m: re.Match) -> str:
        return f"](<{_file_uri(_LINE_SUFFIX_RE.sub('', m.group(1)))}>)"

    return ABS_LINK_TARGET_RE.sub(uri, ABS_ANGLE_LINK_TARGET_RE.sub(uri, line))


def _markdown_link_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _path_link(path: str) -> str:
    target = _LINE_SUFFIX_RE.sub("", path)
    label = Path(path).name or path
    return f"[{_markdown_link_text(label)}](<{_file_uri(target)}>)"


def _looks_like_tree_or_path_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if _ROOT_RE.search(line):
        return True
    if TREE_OR_PATH_HINT_RE.search(line) and not CODELIKE_PUNCT_RE.search(stripped):
        return True
    separators = sum(stripped.count(ch) for ch in "/_-")
    return separators >= 4 and not CODELIKE_PUNCT_RE.search(stripped)


def _take_wrapped_chunk(text: str, max_cells: int) -> tuple[str, str]:
    used = 0
    last_break = -1
    for i, char in enumerate(text):
        used += cell_len(char)
        # Overflow check BEFORE recording this char as a break point — a
        # separator that itself overflows must not become the cut position.
        if used > max_cells:
            if last_break > 0:
                return text[:last_break].rstrip(), text[last_break:].lstrip()
            return "", text
        if char in "/_-" or char.isspace():
            last_break = i + 1
    return text.rstrip(), ""


def _wrap_tree_or_path_line(line: str, max_cells: int) -> list[str]:
    if cell_len(line) <= max_cells or not _looks_like_tree_or_path_line(line):
        return [line]

    indent_len = len(line) - len(line.lstrip(" "))
    indent = line[:indent_len]
    continuation_indent = indent + "  "
    available = max(max_cells - cell_len(indent), 24)
    continuation_available = max(max_cells - cell_len(continuation_indent), 24)
    remaining = line[indent_len:]
    wrapped: list[str] = []

    while remaining and cell_len(indent + remaining) > max_cells:
        chunk, rest = _take_wrapped_chunk(remaining, available)
        if not chunk:
            break
        wrapped.append(indent + chunk)
        remaining = rest
        indent = continuation_indent
        available = continuation_available

    if not wrapped:
        return [line]
    if remaining:
        wrapped.append(indent + remaining)
    return wrapped


def enhance_markdown(
    markdown: str, *, width: int | None = None, only_existing: bool = False
) -> str:
    """Rewrite abs-path link targets to file:// URIs (outside fences only),
    append a "path links:" line after each fence that trapped paths, and wrap
    path-looking fence lines at `width` cells (None = don't wrap). The original
    text is otherwise untouched — path-free markdown passes through
    byte-identical. With only_existing=True, fence-collected paths get a link
    only if they exist on this machine — dead, renamed, container-only, or
    regex-truncated paths are silently skipped (not pure: reads the fs)."""
    code_wrap_width = None if width is None else max(min(width - 8, 88), 28)

    out: list[str] = []
    in_fence = False
    fence_marker = ""
    fence_paths: list[str] = []

    for line in markdown.splitlines():
        stripped = line.lstrip()
        if not in_fence and (stripped.startswith("```") or stripped.startswith("~~~")):
            in_fence = True
            fence_marker = stripped[:3]
            fence_paths = []
            out.append(line)
            continue

        if in_fence:
            if stripped.startswith(fence_marker):
                out.append(line)
                unique_paths = list(dict.fromkeys(fence_paths))
                if only_existing:
                    unique_paths = [
                        p for p in unique_paths
                        if Path(_LINE_SUFFIX_RE.sub("", p)).exists()
                    ]
                if unique_paths:
                    links = " · ".join(_path_link(path) for path in unique_paths[:5])
                    out.append("")
                    out.append(f"> path links: {links}")
                in_fence = False
                fence_marker = ""
                fence_paths = []
                continue
            fence_paths.extend(
                match.group(1).rstrip(".,;:!?") for match in ABS_PATH_RE.finditer(line)
            )
            if code_wrap_width is not None:
                out.extend(_wrap_tree_or_path_line(line, code_wrap_width))
                continue
            out.append(line)
            continue

        # Outside fences only — a fenced markdown example must stay verbatim.
        out.append(_rewrite_link_targets(line))

    return "\n".join(out)
