"""Per-model token pricing + a peak-aware session usage ledger.

Prices are registry DATA: each model's `price.usd` / `price.cny` table in
rockycode/models.toml (and the ~/.rockycode/models.toml override) — each
currency from the provider's OWN published table, never a conversion of the
other. DeepSeek also applies a peak-hour surcharge, described as a named
schedule (`[peak.deepseek]`) that its models point at; other providers carry
no schedule and are never peak-multiplied.

Verified: 2026-09-29 (DeepSeek V4.1-Flash / V4-Pro, USD + CNY tables):
    https://api-docs.deepseek.com/quick_start/pricing        (USD table)
    https://api-docs.deepseek.com/zh-cn/quick_start/pricing   (CNY table)

To update WITHOUT editing the install (survives upgrades), edit
~/.rockycode/models.toml (a model's price / a schedule's holidays) or drop a
~/.rockycode/pricing.toml that overrides the assembled table below. Files on
purpose — never env vars: env is dumpable, the wrong place for maintained
config. `rockycode pricing` prints the live table + these paths.
"""
from __future__ import annotations

import copy
import tomllib
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

PRICING_SOURCE_URL = "https://api-docs.deepseek.com/quick_start/pricing"
PRICING_VERIFIED = "2026-09-29"
OVERRIDE_PATH = Path.home() / ".rockycode" / "pricing.toml"


def default_pricing() -> dict:
    """The price table assembled from the model registry: per-model currency
    tables + the peak schedule each model's provider names. Legacy model ids
    (registry `aliases`) price as their current model, so an old trajectory
    or config still costs out. `peak` (top-level) is DeepSeek's schedule for
    the `rockycode pricing` display; `peaks` holds every schedule by name."""
    from rockycode.engine import providers as P
    data = P.registry_data()
    peaks = {str(k): copy.deepcopy(v) for k, v in (data.get("peak") or {}).items()
             if isinstance(v, dict)}
    models: dict = {}
    for prov in P.catalog().values():
        for mid, spec in prov.specs.items():
            if spec.price:
                models[mid] = {**copy.deepcopy(spec.price), "peak": prov.peak}
        for old, cur in prov.aliases.items():
            if cur in models and old not in models:
                models[old] = copy.deepcopy(models[cur])
    return {
        "peak": copy.deepcopy(peaks.get("deepseek", {"enabled": False})),
        "peaks": peaks,
        "models": models,
        "fallback_model": "deepseek-v4-pro",  # unknown model → price as pro (conservative)
    }


def _deep_merge(base: dict, over: dict) -> None:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def load_pricing(override_path: Path | None = None) -> dict:
    """The registry-assembled table, with ~/.rockycode/pricing.toml merged on
    top if present."""
    pricing = default_pricing()
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
    inside a window. `weekdays_only` and `holidays` (ISO dates, Beijing) make
    weekends and Chinese public holidays off-peak, as DeepSeek bills them.
    False when peak is disabled."""
    if not peak or not peak.get("enabled"):
        return False
    at_utc = at.astimezone(timezone.utc)
    beijing = at_utc + timedelta(hours=8)  # the schedule is a Beijing calendar
    if peak.get("weekdays_only") and beijing.weekday() >= 5:
        return False
    if beijing.date().isoformat() in {str(d) for d in (peak.get("holidays") or [])}:
        return False
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
        # Models that run on the user's OWN machine (ollama etc.) — priced $0
        # by DESIGN, not "unset": they must never trigger the add-a-price nag.
        # Marked at /model-switch/launch time (local model ids are per-machine,
        # discovered live — they can't live in a static table).
        self.free_models: set[str] = set()

    def mark_free(self, model: str) -> None:
        """Declare *model* local: $0 in every currency, counted as configured."""
        self.free_models.add(model)

    def all_free(self) -> bool:
        """Every model used so far is local — the footer shows `· local`
        instead of a cache/price story that only applies to API providers."""
        return bool(self.buckets) and all(
            m in self.free_models for (m, _peak) in self.buckets)

    def add(self, model: str, usage: dict, at: datetime | None = None) -> None:
        """Record a call's usage. *at* (defaults to now) decides peak vs off-peak,
        so each request is priced at the rate in effect when it was made. A
        peak surcharge is a provider's OWN billing scheme — only a model whose
        registry entry names a schedule is ever peak-multiplied."""
        if not usage:
            return
        at = at or datetime.now(timezone.utc)
        peak = _is_peak(at, self._schedule(model))
        b = self.buckets.setdefault((model, peak), {"prompt": 0, "hit": 0, "completion": 0})
        b["prompt"] += usage.get("prompt_tokens", 0) or 0
        b["hit"] += usage.get("prompt_cache_hit_tokens", 0) or 0
        b["completion"] += usage.get("completion_tokens", 0) or 0

    def _schedule(self, model: str) -> dict:
        """The peak schedule this model's provider declares, or {}."""
        name = (self.pricing["models"].get(model) or {}).get("peak")
        return (self.pricing.get("peaks") or {}).get(name, {}) if name else {}

    def priced(self, model: str) -> bool:
        """True if this exact model has its OWN API-fee entry. A model rocky has
        no rate for (a just-added provider) is NOT silently priced as DeepSeek —
        it prices at 0 and flags unset, so cross-provider cost stays honest.
        A local model is priced by definition: $0."""
        return model in self.free_models or model in self.pricing["models"]

    def rate(self, model: str, currency: str = "usd") -> dict:
        """This model's per-1M-token rate in *currency* (in_hit/in_miss/out),
        or the fallback's if unpriced (callers gate on priced())."""
        if model in self.free_models:
            return {"in_hit": 0.0, "in_miss": 0.0, "out": 0.0}
        m = self.pricing["models"].get(model) \
            or self.pricing["models"][self.pricing.get("fallback_model", "deepseek-v4-pro")]
        return m.get(currency) or m["usd"]

    def _rates(self, model: str, currency: str) -> tuple[dict, bool]:
        # Unpriced model → zero rate, flagged unconfigured (not DeepSeek's price):
        # a MiniMax turn must not read as DeepSeek dollars. The user adds the
        # provider's real API fee to ~/.rockycode/pricing.toml.
        if model in self.free_models:  # local — $0 is the CONFIGURED price
            return {"in_hit": 0.0, "in_miss": 0.0, "out": 0.0}, True
        m = self.pricing["models"].get(model)
        if m is None:
            return {"in_hit": 0.0, "in_miss": 0.0, "out": 0.0}, False
        r = m.get(currency)
        if r:
            return r, True
        return m["usd"], False  # currency not configured for this model → fall back + flag

    def cost(self, currency: str = "usd") -> float:
        total = 0.0
        for (model, peak), b in self.buckets.items():
            r, _ = self._rates(model, currency)
            miss = max(0, b["prompt"] - b["hit"])
            base = (miss * r["in_miss"] + b["hit"] * r["in_hit"] + b["completion"] * r["out"]) / 1e6
            mult = float(self._schedule(model).get("multiplier", 1.0)) if peak else 1.0
            total += base * mult
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
