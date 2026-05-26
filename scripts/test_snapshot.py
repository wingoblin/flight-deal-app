"""Tests for the Step 2 judge() and filter_price_outliers().

Pure functions only — no API/DB. Covers the baseline/guard logic and the
cabin-mix protector's distribution-shape behavior.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from snapshot import filter_price_outliers, judge


def _items(*prices):
    return [{"price": p, "airline": "XX"} for p in prices]


class FilterPriceOutliersTests(unittest.TestCase):
    """Drop top 30% by price as a cabin-mix protector."""

    def test_empty_input(self):
        kept, dropped = filter_price_outliers([])
        self.assertEqual(kept, [])
        self.assertEqual(dropped, [])

    def test_single_item_passthrough(self):
        items = _items(100)
        kept, dropped = filter_price_outliers(items)
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])

    def test_drops_top_30_pct_of_10(self):
        # 10 items → keep bottom 7, drop top 3
        kept, dropped = filter_price_outliers(_items(*range(100, 1100, 100)))
        self.assertEqual(len(kept), 7)
        self.assertEqual(dropped, [800, 900, 1000])

    def test_drops_top_30_pct_of_9(self):
        # 9 items × 0.7 = 6.3 → keep 6, drop 3
        kept, dropped = filter_price_outliers(_items(*range(100, 1000, 100)))
        self.assertEqual(len(kept), 6)
        self.assertEqual(dropped, [700, 800, 900])

    def test_cabin_mix_majority_outliers_still_works(self):
        """The whole point: even when contaminating fares are >50% of the
        sample (median itself is contaminated), this filter still removes
        the top 30%. A median-based filter would fail this case."""
        # 5 economy + 5 business (business 10x economy). Median ≈ 3M.
        items = _items(500_000, 600_000, 700_000, 800_000, 900_000,
                       5_000_000, 5_500_000, 6_000_000, 6_500_000, 7_000_000)
        kept, dropped = filter_price_outliers(items)
        self.assertEqual(len(kept), 7)
        # 3 highest dropped
        self.assertEqual(sorted(dropped), [6_000_000, 6_500_000, 7_000_000])

    def test_gmp_ceb_today_scenario(self):
        """GMP→CEB today (n=9): 4 economy-like + 5 business-like fares.
        Top-30% drop removes 3 of the 5 business; downstream the today_n
        guard (>=5) still passes, but baseline comes from history not today."""
        items = _items(730_565, 800_000, 1_014_110, 3_000_000,
                       5_555_074, 5_700_000, 5_800_000, 5_900_000, 5_920_000)
        kept, dropped = filter_price_outliers(items)
        self.assertEqual(len(kept), 6)
        self.assertEqual(min(it["price"] for it in kept), 730_565)
        self.assertEqual(sorted(dropped), [5_800_000, 5_900_000, 5_920_000])

    def test_preserves_nonprice_items_defensively(self):
        items = _items(100, 200, 300, 400, 500) + [{"price": None}]
        kept, dropped = filter_price_outliers(items)
        # 5 valid × 0.7 = 3.5 → keep 3 valid; non-numeric passed through
        self.assertEqual(len([k for k in kept if isinstance(k.get("price"), (int, float))]), 3)
        self.assertEqual(len([k for k in kept if k.get("price") is None]), 1)


class JudgeTests(unittest.TestCase):
    """Baseline = mean(sorted(history)[:5]); three guards."""

    def _stats(self, min_price, n=10):
        return {"n": n, "min": min_price, "p25": 0, "median": 0, "mean": 0}

    def test_history_guard_fires_below_min_history_days(self):
        """<5 days of history → block, baseline None, reason 'history'."""
        # 4 days (< MIN_HISTORY_DAYS=5)
        baseline, discount, is_deal, diag = judge(
            self._stats(100_000), [200_000] * 4, threshold=15.0,
            today_items_count=20,
        )
        self.assertIsNone(baseline)
        self.assertFalse(is_deal)
        self.assertEqual(diag["guard_triggered"], "history")
        self.assertEqual(diag["history_days_used"], 4)

    def test_today_n_guard_fires_below_5(self):
        """today_items_count < 5 → block, reason 'today_n'."""
        baseline, discount, is_deal, diag = judge(
            self._stats(100_000), [200_000] * 10, threshold=15.0,
            today_items_count=4,
        )
        self.assertIsNone(baseline)
        self.assertFalse(is_deal)
        self.assertEqual(diag["guard_triggered"], "today_n")

    def test_normal_pass(self):
        """5+ days history, n>=5, discount within sanity: judge as normal."""
        history = [500_000, 550_000, 600_000, 650_000, 700_000, 800_000]
        # baseline = mean of lowest 5 = (500+550+600+650+700)/5 = 600,000
        # today_min 510,000 → discount = (600k-510k)/600k = 15.0%
        baseline, discount, is_deal, diag = judge(
            self._stats(510_000), history, threshold=15.0,
            today_items_count=20,
        )
        self.assertEqual(baseline, 600_000)
        self.assertAlmostEqual(discount, 15.0, places=4)
        self.assertTrue(is_deal)
        self.assertIsNone(diag["guard_triggered"])

    def test_threshold_strict_inequality(self):
        """discount must be >= threshold to flag."""
        history = [600_000] * 5
        # baseline = 600k, min = 510k, discount = 15%
        # threshold 15 → equal → is_deal True
        _, _, is_deal_eq, _ = judge(self._stats(510_000), history, 15.0, 20)
        self.assertTrue(is_deal_eq)
        # threshold 15.1 → below → is_deal False
        _, _, is_deal_below, _ = judge(self._stats(510_000), history, 15.1, 20)
        self.assertFalse(is_deal_below)

    def test_sanity_guard_blocks_over_50_pct(self):
        """discount > 50% → block (contamination smell), keep baseline/discount
        in diag for forensics."""
        history = [1_000_000] * 5
        # min 100k → discount 90%
        baseline, discount, is_deal, diag = judge(
            self._stats(100_000), history, threshold=15.0,
            today_items_count=20,
        )
        self.assertEqual(baseline, 1_000_000)
        self.assertAlmostEqual(discount, 90.0, places=4)
        self.assertFalse(is_deal)
        self.assertEqual(diag["guard_triggered"], "sanity")

    def test_diag_baseline_method_and_cabin(self):
        """diag carries baseline_method='rolling_n5_lowest' and
        cabin_class='economy' (Step 2-A-5: always economy in phase 1)."""
        history = [500_000] * 5
        _, _, _, diag = judge(self._stats(450_000), history, 15.0, 20)
        self.assertEqual(diag["baseline_method"], "rolling_n5_lowest")
        self.assertEqual(diag["cabin_class"], "economy")

    def test_baseline_uses_only_5_lowest_even_with_more_history(self):
        """Even with 10 days of history, baseline is the mean of the 5 lowest."""
        history = [100, 200, 300, 400, 500, 9999, 9999, 9999, 9999, 9999]
        # lowest 5 = [100,200,300,400,500], mean = 300
        baseline, _, _, _ = judge(self._stats(1), history, 15.0, 20)
        self.assertEqual(baseline, 300)


if __name__ == "__main__":
    unittest.main(verbosity=2)
