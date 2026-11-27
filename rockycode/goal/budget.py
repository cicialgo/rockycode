"""Budget caps for autonomous (goal) mode.

An overnight run stops — with a graceful finalize, never a crash — the moment it
hits ANY configured cap: a spend ceiling (in the user's currency, priced from
the real DeepSeek tables incl. the peak-hour surcharge), a wallclock limit, or a
total-token budget. The user sets any subset or takes the recommended defaults;
before the run we surface the worst-case spend so there are no surprises.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from rockycode.pricing import UsageLedger

# Recommended ceilings per currency (real numbers, not a conversion — the user
# overrides freely). A night, and a spend the user is comfortable leaving alone.
_RECOMMENDED_USD = {"usd": 5.0, "cny": 35.0}
_RECOMMENDED_SECONDS = 8 * 3600


@dataclass
class GoalBudget:
    max_usd: Optional[float] = None       # spend ceiling, in `currency`
    max_seconds: Optional[float] = None   # wallclock limit
    max_tokens: Optional[int] = None      # total (prompt + completion) tokens
    currency: str = "usd"
    _t0: float = field(default=0.0, init=False, repr=False)

    def start(self) -> None:
        self._t0 = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self._t0 if self._t0 else 0.0

    def exceeded(self, ledger: UsageLedger) -> Optional[str]:
        """A human reason if any cap is now hit, else None. Checked each step;
        the caller finalizes gracefully on a non-None result."""
        if self.max_seconds is not None and self.elapsed() >= self.max_seconds:
            return f"wallclock cap ({_dur(self.max_seconds)}) reached"
        if self.max_usd is not None:
            spent = ledger.cost(self.currency)
            if spent >= self.max_usd:
                return (f"spend cap ({_money(self.max_usd, self.currency)}) reached — "
                        f"{_money(spent, self.currency)} spent")
        if self.max_tokens is not None:
            t = ledger.totals()
            if t["prompt"] + t["completion"] >= self.max_tokens:
                return f"token cap ({self.max_tokens:,}) reached"
        return None

    def describe(self) -> str:
        parts = []
        if self.max_usd is not None:
            parts.append(_money(self.max_usd, self.currency))
        if self.max_seconds is not None:
            parts.append(_dur(self.max_seconds))
        if self.max_tokens is not None:
            parts.append(f"{self.max_tokens:,} tokens")
        return " or ".join(parts) if parts else "NO LIMIT (not recommended)"

    def preflight_note(self) -> str:
        """One line shown before the run so the potential spend is explicit."""
        if self.max_usd is not None:
            return f"stops at {self.describe()} — up to {_money(self.max_usd, self.currency)} of API spend"
        if self.max_tokens is not None or self.max_seconds is not None:
            return (f"stops at {self.describe()} — no hard $ cap, so watch the token/time "
                    f"limit (add max_usd for a spend ceiling)")
        return "WARNING: no budget cap set — an overnight run could spend without limit"


def recommended(currency: str = "usd") -> GoalBudget:
    return GoalBudget(
        max_usd=_RECOMMENDED_USD.get(currency, 5.0),
        max_seconds=_RECOMMENDED_SECONDS,
        currency=currency,
    )


def _money(amount: float, currency: str) -> str:
    return f"{'¥' if currency == 'cny' else '$'}{amount:.2f}"


def _dur(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    if h and m:
        return f"{h}h{m}m"
    return f"{h}h" if h else f"{m}m"
