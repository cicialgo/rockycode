"""Bounded, workspace-confined HTML reads; never fetch URLs or execute content."""
import os
import stat
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .messages import HostError

MAX_HTML_BYTES = 1_000_000


def read_artifact(workspace, reference):
    if not isinstance(reference, str) or not reference or len(reference) > 8192:
        raise HostError("diagnostic.artifactPath")
    url = urlsplit(reference)
    # A Windows drive letter is a path, not a URL scheme.
    drive = len(url.scheme) == 1 and reference[1:3] in (":/", ":\\")
    if url.scheme and url.scheme != "file" and not drive:
        raise HostError("diagnostic.artifactWorkspaceOnly")
    if url.scheme == "file" and url.netloc:
        raise HostError("diagnostic.artifactRemote")
    raw = unquote(url.path) if not drive else reference
    root = Path(workspace).resolve(strict=True)
    candidate = Path(raw)
    target = (candidate if candidate.is_absolute() else root / candidate).resolve(strict=True)
    if not target.is_relative_to(root) or target.suffix.lower() not in {".html", ".htm"}:
        raise HostError("diagnostic.artifactWorkspaceOnly")
    # On POSIX open each component relative to an anchored directory fd. Refuse
    # symlinks at open time too, including replacement after resolve().
    if os.name == "posix":
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parts = target.relative_to(root).parts
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        finally:
            os.close(fd)
        handle = os.fdopen(file_fd, "rb")
    else:
        handle = target.open("rb")
    with handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_HTML_BYTES:
            raise HostError("diagnostic.artifactNotRegular")
        data = handle.read(MAX_HTML_BYTES + 1)
    if len(data) > MAX_HTML_BYTES:
        raise HostError("diagnostic.artifactTooLarge")
    return {"path": str(target), "title": target.stem, "html": data.decode("utf-8-sig")}
