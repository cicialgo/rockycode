"""Host messages cross the wire as catalog keys; the renderer localizes.

Every user-facing sentence the Python host produces is a key in the shared
locale files (`rockycode/i18n/locales/<code>.toml`, `[desktop]` section, the
same files the desktop's generated catalog comes from). The host sends the key
plus its parameters and the English text as a fallback for a client that does
not know the key; it never sends a sentence the renderer has to match.
"""
from __future__ import annotations

try:  # the shared catalogs live in the core; fixture mode runs without it
    from rockycode.i18n import tr as _tr
except ImportError:  # pragma: no cover — exercised by the desktop CI without the core
    _tr = None


def text(key: str, **params: object) -> str:
    """The English text for a `desktop.*` key (prefix implied), or the key."""
    if _tr is None:
        return key
    return _tr("desktop." + key, "en", **params)


class HostError(ValueError):
    """A refusal the client shows: `key` (desktop catalog key) + `params`."""

    def __init__(self, key: str, **params: object) -> None:
        self.key = key
        self.params = params
        super().__init__(text(key, **params))


def error_payload(exc: BaseException) -> dict:
    """The JSON-RPC `error` object for an exception raised while serving."""
    if isinstance(exc, HostError):
        return {"code": -32602, "message": str(exc), "data": {"key": exc.key, "params": exc.params}}
    if isinstance(exc, ValueError):
        # Plain ValueErrors are the host's own, already English and safe to show.
        return {"code": -32602, "message": str(exc), "data": {"key": None, "params": {}}}
    key = "diagnostic.requestProcessing"
    return {"code": -32602, "message": text(key), "data": {"key": key, "params": {}}}


def keyed(key: str, **params: object) -> dict:
    """A persisted message/lane error: English `error` + `error_key` + `error_params`."""
    return {"error": text(key, **params), "error_key": key, "error_params": params}
