"""Tests for the Step 2 judge() and filter_price_outliers().

Pure functions only — no API/DB. Covers the baseline/guard logic and the
cabin-mix protector's distribution-shape behavior.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from snapshot import _guard_publish_safety, filter_price_outliers, judge


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
        baseline, discount, is_deal, diag = judge(
            self._stats(100_000), [200_000] * 4, today_items_count=20,
        )
        self.assertIsNone(baseline)
        self.assertFalse(is_deal)
        self.assertEqual(diag["guard_triggered"], "history")
        self.assertEqual(diag["history_days_used"], 4)

    def test_today_n_guard_fires_below_5(self):
        """today_items_count < 5 → block, reason 'today_n'."""
        baseline, discount, is_deal, diag = judge(
            self._stats(100_000), [200_000] * 10, today_items_count=4,
        )
        self.assertIsNone(baseline)
        self.assertFalse(is_deal)
        self.assertEqual(diag["guard_triggered"], "today_n")

    def test_green_tier_below_floor(self):
        """min cheaper than the floor → deal, tier green (no lower bound)."""
        history = [500_000, 550_000, 600_000, 650_000, 700_000, 800_000]
        # baseline = mean(lowest 5) = 600,000
        baseline, discount, is_deal, diag = judge(
            self._stats(540_000), history, today_items_count=20,
        )
        self.assertEqual(baseline, 600_000)
        self.assertGreater(discount, 0)   # below floor → positive discount
        self.assertTrue(is_deal)
        self.assertEqual(diag["tier"], "green")
        self.assertIsNone(diag["guard_triggered"])

    def test_orange_tier_floor_to_5pct(self):
        """floor .. floor+5% → deal, tier orange (boundaries inclusive)."""
        history = [600_000] * 5            # baseline 600,000; +5% = 630,000
        # exactly at the floor → orange (green is strictly below)
        _, discount, is_deal, diag = judge(self._stats(600_000), history, 20)
        self.assertTrue(is_deal)
        self.assertAlmostEqual(discount, 0.0, places=4)
        self.assertEqual(diag["tier"], "orange")
        # +5% exact → still orange
        _, _, _, diag5 = judge(self._stats(630_000), history, 20)
        self.assertEqual(diag5["tier"], "orange")

    def test_red_tier_5_to_20pct(self):
        """floor+5% .. floor+20% → deal, tier red (upper edge inclusive)."""
        history = [600_000] * 5            # +5% = 630,000, +20% = 720,000
        # just over +5% → red
        _, _, is_deal, diag = judge(self._stats(630_001), history, 20)
        self.assertTrue(is_deal)
        self.assertEqual(diag["tier"], "red")
        # +20% exact → still a deal, red
        _, _, is_deal_edge, diag20 = judge(self._stats(720_000), history, 20)
        self.assertTrue(is_deal_edge)
        self.assertEqual(diag20["tier"], "red")

    def test_no_deal_above_red(self):
        """min above floor+20% → not a deal, tier None, no guard."""
        history = [600_000] * 5            # +20% = 720,000
        baseline, discount, is_deal, diag = judge(
            self._stats(720_001), history, today_items_count=20,
        )
        self.assertEqual(baseline, 600_000)
        self.assertLess(discount, 0)
        self.assertFalse(is_deal)
        self.assertIsNone(diag["tier"])
        self.assertIsNone(diag["guard_triggered"])

    def test_sanity_guard_blocks_far_below_floor(self):
        """discount > 50% (min far below floor) → block as contamination."""
        history = [1_000_000] * 5
        baseline, discount, is_deal, diag = judge(
            self._stats(100_000), history, today_items_count=20,
        )
        self.assertEqual(baseline, 1_000_000)
        self.assertAlmostEqual(discount, 90.0, places=4)
        self.assertFalse(is_deal)
        self.assertEqual(diag["guard_triggered"], "sanity")

    def test_diag_baseline_method_and_cabin(self):
        history = [500_000] * 5
        _, _, _, diag = judge(self._stats(450_000), history, 20)
        self.assertEqual(diag["baseline_method"], "rolling_n5_lowest")
        self.assertEqual(diag["cabin_class"], "economy")

    def test_baseline_uses_only_5_lowest_even_with_more_history(self):
        history = [100, 200, 300, 400, 500, 9999, 9999, 9999, 9999, 9999]
        baseline, _, _, _ = judge(self._stats(1), history, 20)
        self.assertEqual(baseline, 300)


class HistoricalMinsWindowTests(unittest.TestCase):
    """dealdb.historical_mins rolling-window filter (in-memory sqlite)."""

    def _conn_with(self, rows):
        import dealdb
        conn = dealdb.connect(":memory:")
        for date_str, mn in rows:
            dealdb.upsert_snapshot(conn, {
                "snapshot_date": date_str, "origin": "ICN", "destination": "BKK",
                "trip": "roundtrip", "n": 10, "min_price": mn,
                "p25": mn, "median": mn, "mean": mn,
                "cheapest_depart_at": None, "cheapest_return_at": None,
                "cheapest_airline": None, "cheapest_gate": None, "cheapest_link": None,
            })
        return conn, dealdb

    def test_window_excludes_old_rows(self):
        # today = 2026-05-30; 30-day window keeps >= 2026-04-30
        conn, dealdb = self._conn_with([
            ("2026-03-01", 100),   # 90d old — excluded
            ("2026-04-15", 200),   # 45d old — excluded
            ("2026-05-10", 300),   # within window
            ("2026-05-20", 400),   # within window
        ])
        mins = dealdb.historical_mins(conn, "ICN", "BKK", "roundtrip",
                                      "2026-05-30", window_days=30)
        self.assertEqual(sorted(mins), [300, 400])

    def test_no_window_returns_all_history(self):
        conn, dealdb = self._conn_with([
            ("2026-03-01", 100), ("2026-05-20", 400),
        ])
        mins = dealdb.historical_mins(conn, "ICN", "BKK", "roundtrip", "2026-05-30")
        self.assertEqual(sorted(mins), [100, 400])

    def test_window_under_30_days_uses_what_exists(self):
        # Only 3 days of data, all within window → all returned
        conn, dealdb = self._conn_with([
            ("2026-05-25", 100), ("2026-05-26", 200), ("2026-05-27", 300),
        ])
        mins = dealdb.historical_mins(conn, "ICN", "BKK", "roundtrip",
                                      "2026-05-28", window_days=30)
        self.assertEqual(sorted(mins), [100, 200, 300])


class GuardPublishSafetyTests(unittest.TestCase):
    """Abort before publishing on broken runs so a good feed isn't overwritten."""

    def _ok(self, is_deal):
        return {"status": "ok", "is_deal": is_deal}

    def _err(self):
        return {"status": "error: <HTTPError 500>"}

    def test_high_error_rate_aborts(self):
        # 6 errors / 10 = 60% > 50% → abort even though some deals exist
        results = [self._err() for _ in range(6)] + [self._ok(True) for _ in range(4)]
        with self.assertRaises(SystemExit) as cm:
            _guard_publish_safety(results)
        self.assertEqual(cm.exception.code, 3)

    def test_zero_deals_aborts(self):
        # error rate fine (0%) but no deals → abort (don't publish empty feed)
        results = [self._ok(False) for _ in range(10)]
        with self.assertRaises(SystemExit) as cm:
            _guard_publish_safety(results)
        self.assertEqual(cm.exception.code, 4)

    def test_healthy_run_passes(self):
        # low error rate + deals present → no abort
        results = [self._err()] + [self._ok(True) for _ in range(9)]
        _guard_publish_safety(results)  # should not raise

    def test_crosscheck_keeps_dont_count_as_errors(self):
        # realtime-crosscheck failures keep candidates with status "ok"; only
        # data-API failures carry an "error" status. A run that is all-ok with
        # deals must pass regardless of crosscheck outcomes.
        results = [self._ok(True) for _ in range(20)]
        _guard_publish_safety(results)  # should not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
