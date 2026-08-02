"""Read an IMAGE (or text) from the system clipboard — the paste-in side.

Terminals only ever deliver *text* through bracketed paste: hitting Cmd+V on
a copied screenshot pastes nothing. So "paste an image" has to reach around
the terminal and ask the OS clipboard directly, the same way _pbcopy in
app.py already writes it. All subprocess, zero third-party deps:

macOS (three clipboard flavors, tried in order):
  1. a copied FILE (Finder ⌘C) → the clipboard is a file URL; if it points
     at an image we just use that path, no bytes copied;
  2. PNG data (screenshots, most browsers) → AppleScript writes it to a file;
  3. TIFF data (Preview and some apps offer only TIFF) → written then
     converted with `sips` (ships with macOS) since no provider takes TIFF.
Linux: wl-paste (Wayland) or xclip (X11), image/png target.

Everything here BLOCKS on subprocesses — call it via asyncio.to_thread from
the TUI. Failures return None: "nothing to paste" is a notify(), never a crash.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from rockycode.engine.images import IMAGE_EXTS

_TIMEOUT = 5  # s — clipboard reads are local and fast; a hang must not freeze paste


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT, **kw)


def _fresh(dest_dir: Path, ext: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return dest_dir / f"paste-{stamp}-{uuid.uuid4().hex[:6]}{ext}"


def _mac_copied_file() -> Path | None:
    """Flavor 1: the clipboard holds a file reference (Finder ⌘C on a file)."""
    r = _run(["osascript", "-e", "POSIX path of (the clipboard as «class furl»)"])
    if r.returncode != 0:
        return None
    p = Path(r.stdout.decode().strip())
    if p.suffix.lower() in IMAGE_EXTS and p.is_file():
        return p
    return None


def _mac_image_data(dest_dir: Path) -> Path | None:
    """Flavors 2+3: raw image data on the clipboard, written out by AppleScript.
    The data is coerced BEFORE the file is opened, so a text-only clipboard
    errors out without leaving an empty file behind."""
    for cls, ext in (("PNGf", ".png"), ("TIFF", ".tiff")):
        dest = _fresh(dest_dir, ext)
        script = (
            f"set d to (the clipboard as «class {cls}»)\n"
            f'set f to open for access POSIX file "{dest}" with write permission\n'
            "set eof f to 0\n"
            "write d to f\n"
            "close access f"
        )
        r = _run(["osascript", "-e", script])
        if r.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            continue
        if ext == ".tiff":  # no provider takes TIFF — convert with macOS's own sips
            png = dest.with_suffix(".png")
            r = _run(["sips", "-s", "format", "png", str(dest), "--out", str(png)])
            dest.unlink(missing_ok=True)
            if r.returncode != 0 or not png.is_file() or png.stat().st_size == 0:
                png.unlink(missing_ok=True)
                return None
            return png
        return dest
    return None


def _linux_image(dest_dir: Path) -> Path | None:
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        cmd = ["wl-paste", "-t", "image/png"]
    elif shutil.which("xclip"):
        cmd = ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]
    else:
        return None
    r = _run(cmd)
    if r.returncode != 0 or not r.stdout:
        return None
    dest = _fresh(dest_dir, ".png")
    dest.write_bytes(r.stdout)
    return dest


def grab_image(dest_dir: Path) -> Path | None:
    """The image on the system clipboard, as a file path — a directly usable
    existing file (copied in Finder) or a fresh file saved into *dest_dir*.
    None when the clipboard has no image (or the platform has no reader)."""
    try:
        if sys.platform == "darwin":
            return _mac_copied_file() or _mac_image_data(dest_dir)
        if sys.platform.startswith("linux"):
            return _linux_image(dest_dir)
    except Exception:  # noqa: BLE001 — clipboard weirdness must never crash paste
        return None
    return None


def grab_text() -> str | None:
    """Clipboard text — the Ctrl+V fallback when there's no image, so the key
    always does *something* pastey instead of dying with a shrug."""
    try:
        if sys.platform == "darwin" and shutil.which("pbpaste"):
            r = _run(["pbpaste"])
        elif os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
            r = _run(["wl-paste", "--no-newline"])
        elif shutil.which("xclip"):
            r = _run(["xclip", "-selection", "clipboard", "-o"])
        else:
            return None
        if r.returncode != 0:
            return None
        text = r.stdout.decode(errors="replace")
        return text if text else None
    except Exception:  # noqa: BLE001
        return None
