"""Offline, stdlib-only search fee regressions; no credentials or network."""
import unittest
from datetime import datetime, timezone

from rockycode.pricing import UsageLedger


def ledger():
    return UsageLedger(pricing={
        "models": {"deepseek-flash": {"usd": {"in_miss": 1, "in_hit": 0, "out": 1},
                                     "cny": {"in_miss": 5, "in_hit": 0, "out": 5},
                                     "peak": "ds"}},
        "peaks": {"ds": {"enabled": True, "multiplier": 2,
                          "windows_utc": [{"start": "00:00", "end": "23:59"}]}},
    })


class SearchPricingTests(unittest.TestCase):
    def test_official_rates(self):
        led = ledger()
        self.assertEqual(led.cost(), 0)
        led.add_deepseek_search(1000)
        self.assertAlmostEqual(led.cost("usd"), .75)
        self.assertAlmostEqual(led.cost("cny"), 5)

    def test_peak_only_multiplies_tokens(self):
        led = ledger()
        led.add("deepseek-flash", {"prompt_tokens": 1_000_000},
                at=datetime(2026, 11, 2, 7, tzinfo=timezone.utc))
        led.add_deepseek_search(1000)
        self.assertAlmostEqual(led.cost(), 2 + .75)
        self.assertAlmostEqual(led.cost("cny"), 10 + 5)
        self.assertEqual(led.totals()["prompt"], 1_000_000)

    def test_accumulation_and_zero(self):
        led = ledger()
        for count in (2, 0, 3):
            led.add_deepseek_search(count)
        self.assertEqual(led.search_requests, 5)
        self.assertAlmostEqual(led.cost(), .00375)
        self.assertTrue(led.configured("usd"))
        self.assertFalse(led.configured("eur"))
        self.assertFalse(led.all_free())

    def test_unknown_never_claims_complete(self):
        for count in (None, True, -1, 1.5, "2"):
            with self.subTest(count=count):
                led = ledger()
                led.add_deepseek_search(count)
                led.add_deepseek_search(2)
                self.assertFalse(led.search_usage_complete)
                self.assertFalse(led.configured("usd"))
                self.assertAlmostEqual(led.cost(), .0015)


if __name__ == "__main__":
    unittest.main()
