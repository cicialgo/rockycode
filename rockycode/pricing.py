"""Per-model token pricing + a peak-aware session usage ledger.

DeepSeek publishes SEPARATE CNY and USD price tables (each set independently —
NOT a conversion of the other), and applies a peak-hour surcharge. So we keep
BOTH currencies' official numbers and price in whichever the user picked; we
never convert one into the other. Verify + update the numbers at the source when
DeepSeek changes them:

    https://api-docs.deepseek.com/quick_start/pricing        (USD table)
    https://api-docs.deepseek.com/zh-cn/quick_start/pricing   (CNY table)

Verified: 2026-07-04 (per 1,000,000 tokens).

To update WITHOUT editing the install (survives upgrades), drop a
~/.rockycode/pricing.toml that overrides any values below. It is a FILE on
purpose — never an env var: env is dumpable (and now redacted), the wrong place
for maintained config. `rockycode pricing` prints the live table + this path.
"""
from __future__ import annotations

import copy
import tomllib
from datetime import datetime, time as dtime, timezone
from pathlib import Path

PRICING_SOURCE_URL = "https://api-docs.deepseek.com/quick_start/pricing"
PRICING_VERIFIED = "2026-07-04"
OVERRIDE_PATH = Path.home() / ".rockycode" / "pricing.toml"

# Official list prices, per 1M tokens, each currency from its OWN table.
DEFAULT_PRICING: dict = {
    "peak": {
        # DeepSeek peak-valley pricing (announced): peak-hour rate = 2x regular,
        # for ALL billing items, during these UTC windows — 01:00–04:00 and
        # 06:00–10:00. It starts MID-JULY 2026, so the logic is wired but gated
        # behind effective_date: before that date nothing is surcharged even
        # inside a window, and it auto-activates on the date with no code change.
        # Confirm the exact start date and adjust here or in the override file.
        "enabled": True,
        "effective_date": "2026-07-15",  # UTC; peak surcharge does not apply before this
        "multiplier": 2.0,
        "windows_utc": [
            {"start": "01:00", "end": "04:00"},
            {"start": "06:00", "end": "10:00"},
        ],
    },
    "models": {
        "deepseek-v4-pro": {
            "usd": {"in_hit": 0.003625, "in_miss": 0.435, "out": 0.87},
            "cny": {"in_hit": 0.025, "in_miss": 3.0, "out": 6.0},
        },
        "deepseek-v4-flash": {
            "usd": {"in_hit": 0.0028, "in_miss": 0.14, "out": 0.28},
            "cny": {"in_hit": 0.02, "in_miss": 1.0, "out": 2.0},
        },
    },
    "fallback_model": "deepseek-v4-pro",  # unknown model → price as pro (conservative)
}


def _deep_merge(base: dict, over: dict) -> None:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def load_pricing(override_path: Path | None = None) -> dict:
    """Built-in defaults, with ~/.rockycode/pricing.toml merged on top if present."""
    pricing = copy.deepcopy(DEFAULT_PRICING)
    path = OVERRIDE_PATH if override_path is None else override_path
    if path.exists():
        try:
            _deep_merge(pricing, tomllib.loads(path.read_text()))
        except (tomllib.TOMLDecodeError, OSError):
            pass  # a broken override must never break the cost display
    return pricing


def _hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _is_peak(at: datetime, peak: dict) -> bool:
    """Is *at* inside a peak window AND on/after the effective date? Peak-valley
    pricing starts mid-July, so before effective_date nothing is surcharged even
    inside a window. False when peak is disabled."""
    if not peak or not peak.get("enabled"):
        return False
    at_utc = at.astimezone(timezone.utc)
    eff = peak.get("effective_date")
    if eff:
        try:
            if at_utc < datetime.fromisoformat(eff).replace(tzinfo=timezone.utc):
                return False  # peak-valley pricing not yet in effect
        except ValueError:
            pass
    now = at_utc.time()
    for w in peak.get("windows_utc", []):
        start, end = _hhmm(w["start"]), _hhmm(w["end"])
        inside = (start <= now < end) if start <= end else (now >= start or now < end)
        if inside:
            return True
    return False


class UsageLedger:
    """Accumulates token usage per (model, peak?) so cost() can price each
    currency from its own table and apply the peak multiplier per request."""

    def __init__(self, pricing: dict | None = None) -> None:
        self.pricing = pricing if pricing is not None else load_pricing()
        self.buckets: dict[tuple[str, bool], dict] = {}  # (model, is_peak) -> counts

    def add(self, model: str, usage: dict, at: datetime | None = None) -> None:
        """Record a call's usage. *at* (defaults to now) decides peak vs off-peak,
        so each request is priced at the rate in effect when it was made. The
        peak surcharge is DeepSeek's OWN billing scheme, so it applies only to
        DeepSeek models — a MiniMax/GLM turn is never peak-multiplied."""
        if not usage:
            return
        at = at or datetime.now(timezone.utc)
        peak = model.startswith("deepseek") and _is_peak(at, self.pricing.get("peak", {}))
        b = self.buckets.setdefault((model, peak), {"prompt": 0, "hit": 0, "completion": 0})
        b["prompt"] += usage.get("prompt_tokens", 0) or 0
        b["hit"] += usage.get("prompt_cache_hit_tokens", 0) or 0
        b["completion"] += usage.get("completion_tokens", 0) or 0

    def priced(self, model: str) -> bool:
        """True if this exact model has its OWN API-fee entry. A model rocky has
        no rate for (a just-added provider) is NOT silently priced as DeepSeek —
        it prices at 0 and flags unset, so cross-provider cost stays honest."""
        return model in self.pricing["models"]

    def rate(self, model: str, currency: str = "usd") -> dict:
        """This model's per-1M-token rate in *currency* (in_hit/in_miss/out),
        or the fallback's if unpriced (callers gate on priced())."""
        m = self.pricing["models"].get(model) \
            or self.pricing["models"][self.pricing.get("fallback_model", "deepseek-v4-pro")]
        return m.get(currency) or m["usd"]

    def _rates(self, model: str, currency: str) -> tuple[dict, bool]:
        # Unpriced model → zero rate, flagged unconfigured (not DeepSeek's price):
        # a MiniMax turn must not read as DeepSeek dollars. The user adds the
        # provider's real API fee to ~/.rockycode/pricing.toml.
        m = self.pricing["models"].get(model)
        if m is None:
            return {"in_hit": 0.0, "in_miss": 0.0, "out": 0.0}, False
        r = m.get(currency)
        if r:
            return r, True
        return m["usd"], False  # currency not configured for this model → fall back + flag

    def cost(self, currency: str = "usd") -> float:
        mult = self.pricing.get("peak", {}).get("multiplier", 1.0)
        total = 0.0
        for (model, peak), b in self.buckets.items():
            r, _ = self._rates(model, currency)
            miss = max(0, b["prompt"] - b["hit"])
            base = (miss * r["in_miss"] + b["hit"] * r["in_hit"] + b["completion"] * r["out"]) / 1e6
            total += base * (mult if peak else 1.0)
        return total

    def cost_usd(self) -> float:  # back-compat alias
        return self.cost("usd")

    def configured(self, currency: str) -> bool:
        """True if every model used has its OWN rates for *currency* (no fallback)."""
        return all(self._rates(model, currency)[1] for (model, _peak) in self.buckets)

    def totals(self) -> dict:
        agg = {"prompt": 0, "hit": 0, "completion": 0}
        for b in self.buckets.values():
            for k in agg:
                agg[k] += b[k]
        return agg
