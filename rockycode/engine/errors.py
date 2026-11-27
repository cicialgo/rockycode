"""A failed model request, as one plain sentence with the next step.

What the SDK raises reads like `AuthenticationError: Error code: 401 -
{'error': {'message': '…', 'type': 'authentication_error'}}`: a class name and
a dict, with nothing to do next. That text stays available as the event's
`detail`. What a person reads first is what happened and what to do, and
`kind` lets each front end say it in its own voice and language.

Nothing here quotes the provider's body: the sentences are built from the
status code, the host, the model id and the key's source name — never the key.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# The kinds a front end can word for itself. "unknown" keeps the raw text.
KINDS = ("auth", "credit", "rate_limit", "timeout", "network", "context", "model", "request", "server", "unknown")

_CONTEXT = ("context length", "context_length", "maximum context", "context window", "too many tokens",
            "reduce the length", "prompt is too long", "input is too long")
_CREDIT = ("insufficient balance", "insufficient_balance", "insufficient_quota", "insufficient credit",
           "out of credit", "billing", "arrears", "payment required")
_MODEL = ("model_not_found", "model not found", "model not exist", "does not exist", "no such model", "unknown model")


@dataclass(frozen=True)
class Explained:
    kind: str
    message: str   # one neutral English sentence and the next step
    params: dict = field(default_factory=dict)  # status · host · model · key_source — facts a front end may reuse


def explain(error: BaseException, *, model: str, host: str, key_source: str = "") -> Explained:
    """Classify a failed request by what the user can do about it."""
    status: Optional[int] = getattr(error, "status_code", None)
    if not isinstance(status, int) or isinstance(status, bool):
        status = None
    name = type(error).__name__
    text = str(error).lower()
    where = f" · key source: {key_source}" if key_source else ""
    params = {"status": status or "", "host": host, "model": model, "key_source": key_source}

    def said(kind: str, message: str) -> Explained:
        return Explained(kind, message, params)

    if status == 402 or any(mark in text for mark in _CREDIT):
        return said("credit", f"The provider account is out of credit ({status or 402}){where}. "
                              "Top up at the provider, or switch models.")
    if status in (401, 403) or name in ("AuthenticationError", "PermissionDeniedError"):
        return said("auth", f"The key was rejected ({status or 401}){where}. Add a working key for this provider.")
    if status == 429 or name == "RateLimitError":
        return said("rate_limit", "The provider is rate limiting this key (429). Wait a little, then send again.")
    if name == "APITimeoutError" or isinstance(error, TimeoutError):
        return said("timeout", f"{host} did not answer in time. Send again, or switch models.")
    if name == "APIConnectionError" or isinstance(error, ConnectionError):
        return said("network", f"Could not reach {host}. Check the network, then send again.")
    if status in (400, 413, 422) and any(mark in text for mark in _CONTEXT):
        return said("context", f"The conversation is too long for {model}. Start a new task to continue.")
    if status == 404 or (status in (400, 422) and any(mark in text for mark in _MODEL)):
        return said("model", f"The provider does not know the model {model} ({status}). Pick another model.")
    if status in (400, 422):
        # Some providers answer a malformed or foreign key with 400: keep the key's source in sight.
        return said("request", f"The provider refused the request ({status}){where}.")
    if status is not None and status >= 500:
        return said("server", f"The provider had a problem ({status}). Send again in a moment.")
    return said("unknown", f"{name}: {error}")
