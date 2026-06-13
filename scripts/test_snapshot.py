"""Tests for the Step 2 judge() and filter_price_outliers().

Pure functions only — no API/DB. Covers the baseline/guard logic and the
cabin-mix protector's distribution-shape behavior.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import datetime as dt

from config import (
    DEAL_THRESHOLD_PCT,
    DISPLAY_SAFETY_BUFFER_PCT,
    LIVE_SAFETY_BUFFER_PCT,
    REGIONAL_ORIGINS,
    guards_for_origin,
)
from snapshot import (
    _guard_publish_safety,
    apply_conservative_pricing,
    filter_price_outliers,
    is_stale,
    judge,
)


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
    """Baseline = median(history); deal = min <= baseline*(1-threshold); guards."""

    def _stats(self, min_price, n=10):
        return {"n": n, "min": min_price, "p25": 0, "median": 0, "mean": 0}

    def test_history_guard_fires_below_min_history_days(self):
        """<MIN_HISTORY_DAYS (3) days of history → block, reason 'history'."""
        baseline, discount, is_deal, diag = judge(
            self._stats(100_000), [200_000] * 2, today_items_count=20,
        )
        self.assertIsNone(baseline)
        self.assertFalse(is_deal)
        self.assertEqual(diag["guard_triggered"], "history")
        self.assertEqual(diag["history_days_used"], 2)

    def test_history_guard_passes_at_min_history_days(self):
        """exactly MIN_HISTORY_DAYS (3) days → no history guard."""
        _, _, _, diag = judge(
            self._stats(100_000), [200_000] * 3, today_items_count=20,
        )
        self.assertNotEqual(diag["guard_triggered"], "history")

    def test_today_n_guard_fires_below_min(self):
        """today_items_count < MIN_TODAY_FARES (3) → block, reason 'today_n'."""
        baseline, discount, is_deal, diag = judge(
            self._stats(100_000), [200_000] * 10, today_items_count=2,
        )
        self.assertIsNone(baseline)
        self.assertFalse(is_deal)
        self.assertEqual(diag["guard_triggered"], "today_n")

    def test_today_n_guard_passes_at_min(self):
        """today_items_count == MIN_TODAY_FARES (3) → no today_n guard."""
        _, _, _, diag = judge(
            self._stats(100_000), [200_000] * 10, today_items_count=3,
        )
        self.assertNotEqual(diag["guard_triggered"], "today_n")

    def test_deal_below_typical(self):
        """min at least DEAL_THRESHOLD_PCT below the typical (median) → deal."""
        history = [500_000, 550_000, 600_000, 650_000, 700_000]  # median 600,000
        baseline, discount, is_deal, diag = judge(
            self._stats(500_000), history, today_items_count=20,
        )
        self.assertEqual(baseline, 600_000)
        self.assertGreater(discount, 0)   # below typical → positive discount
        self.assertTrue(is_deal)
        self.assertIsNone(diag["guard_triggered"])

    def test_threshold_edge_inclusive(self):
        """min exactly DEAL_THRESHOLD_PCT below typical → deal (inclusive)."""
        history = [600_000] * 5
        cutoff = int(600_000 * (1 - DEAL_THRESHOLD_PCT / 100))  # 7% → 558,000
        _, _, is_deal, _ = judge(self._stats(cutoff), history, 20)
        self.assertTrue(is_deal)

    def test_at_typical_not_a_deal(self):
        """min at the typical price isn't cheap enough → not a deal, no guard."""
        history = [600_000] * 5
        baseline, discount, is_deal, diag = judge(
            self._stats(600_000), history, today_items_count=20,
        )
        self.assertEqual(baseline, 600_000)
        self.assertAlmostEqual(discount, 0.0, places=4)
        self.assertFalse(is_deal)
        self.assertIsNone(diag["guard_triggered"])

    def test_just_above_threshold_not_a_deal(self):
        """min just under DEAL_THRESHOLD_PCT below typical → not a deal, no guard."""
        history = [600_000] * 5
        cutoff = int(600_000 * (1 - DEAL_THRESHOLD_PCT / 100))
        _, _, is_deal, diag = judge(self._stats(cutoff + 1_000), history, 20)
        self.assertFalse(is_deal)
        self.assertIsNone(diag["guard_triggered"])

    def test_sanity_guard_blocks_far_below_typical(self):
        """discount > 50% (min far below typical) → block as contamination."""
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
        self.assertEqual(diag["baseline_method"], "rolling_median")
        self.assertEqual(diag["cabin_class"], "economy")

    def test_baseline_is_median_of_history(self):
        """Baseline is the median of all daily mins (robust to high outliers)."""
        history = [100, 200, 300, 400, 500, 9999, 9999]  # median = 400
        baseline, _, _, _ = judge(self._stats(1), history, 20)
        self.assertEqual(baseline, 400)


class RegionalGuardTests(unittest.TestCase):
    """Regional origins get relaxed history/today-fare guards (2 vs 3); major
    hubs stay strict. Thin samples never let business-class noise in because the
    judged/shown values use the cheapest fare and the lowest-N daily mins."""

    def _stats(self, min_price, n=10):
        return {"n": n, "min": min_price, "p25": 0, "median": 0, "mean": 0}

    def test_major_hubs_strict_regional_relaxed(self):
        for o in ("ICN", "GMP"):
            self.assertEqual(guards_for_origin(o), (3, 3))
        for o in REGIONAL_ORIGINS:
            self.assertEqual(guards_for_origin(o), (2, 2))

    def test_regional_origins_membership(self):
        self.assertEqual(REGIONAL_ORIGINS, {"PUS", "TAE", "CJU", "CJJ", "MWX"})

    def test_two_day_history_blocks_major_passes_regional(self):
        """2 days of history + 2 fares: blocked for ICN, surfaces for a regional
        origin under the relaxed guards."""
        # median([100k,105k]) = 102,500; -7% cutoff = 95,325, so min 90k is a deal.
        stats, history = self._stats(90_000, n=2), [100_000, 105_000]

        icn = judge(stats, history, stats["n"], *guards_for_origin("ICN"))
        self.assertFalse(icn[2])
        self.assertEqual(icn[3]["guard_triggered"], "history")

        tae = judge(stats, history, stats["n"], *guards_for_origin("TAE"))
        self.assertTrue(tae[2])
        self.assertIsNone(tae[3]["guard_triggered"])

    def test_relaxed_guard_still_blocks_below_two(self):
        """Even relaxed, a single day / single fare is too thin and stays blocked."""
        stats = self._stats(100_000, n=1)
        one_day = judge(stats, [100_000], stats["n"], *guards_for_origin("CJU"))
        self.assertEqual(one_day[3]["guard_triggered"], "history")

    def test_thin_economy_sample_surfaces_clean(self):
        """All-economy thin regional sample → judged on the economy min, real
        deal, no noise. baseline = median of the daily mins."""
        history = [90_000, 95_000, 100_000]
        stats = self._stats(80_000, n=2)
        baseline, discount, is_deal, _ = judge(
            stats, history, stats["n"], *guards_for_origin("CJU"))
        self.assertEqual(baseline, 95_000)
        self.assertLess(discount, 50.0)
        self.assertTrue(is_deal)

    def test_median_baseline_robust_to_business_day(self):
        """In a thin sample a business-contaminated day is a high outlier; the
        MEDIAN baseline ignores it (a mean would not), so the baseline isn't
        distorted and the economy min is judged against a clean typical price."""
        history = [90_000, 95_000, 600_000]  # 600k = business-contaminated day
        stats = self._stats(85_000, n=2)
        baseline, discount, is_deal, diag = judge(
            stats, history, stats["n"], *guards_for_origin("TAE"))
        self.assertEqual(baseline, 95_000)        # median ignores the 600k outlier
        self.assertIsNone(diag["guard_triggered"])
        self.assertLess(discount, 50.0)
        # 85,000 <= 95,000 * (1 - 7%) = 88,350 → genuine deal on a clean baseline
        self.assertTrue(is_deal)


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


class IsStaleTests(unittest.TestCase):
    """Freshness gate honors the per-call max_age_days (sparse-route fallback)."""

    NOW = dt.datetime(2026, 6, 11, tzinfo=dt.timezone.utc)

    def _found(self, days_ago):
        return {"found_at": (self.NOW - dt.timedelta(days=days_ago)).isoformat()}

    def test_actual_false_always_stale(self):
        self.assertTrue(is_stale({"actual": False}, self.NOW, max_age_days=30))

    def test_within_window_is_fresh(self):
        # 5 days old, strict 2-day window → stale; 30-day window → fresh
        self.assertTrue(is_stale(self._found(5), self.NOW, max_age_days=2))
        self.assertFalse(is_stale(self._found(5), self.NOW, max_age_days=30))

    def test_beyond_wide_window_still_stale(self):
        self.assertTrue(is_stale(self._found(40), self.NOW, max_age_days=30))

    def test_missing_found_at_not_stale(self):
        self.assertFalse(is_stale({}, self.NOW, max_age_days=2))


class ApplyConservativePricingTests(unittest.TestCase):
    """Published price = max(cache, live) + adaptive buffer (LIVE when a live
    cross-check fare exists, else DISPLAY), rounded up to 1,000 KRW."""

    def _deal(self, **over):
        r = {
            "status": "ok",
            "is_deal": True,
            "min": 100_000,
            "baseline": 120_000,
            "discount": 16.7,
        }
        r.update(over)
        return r

    def _buffered_round(self, anchor, live=False):
        import math
        pct = LIVE_SAFETY_BUFFER_PCT if live else DISPLAY_SAFETY_BUFFER_PCT
        return math.ceil(anchor * (1 + pct / 100) / 1000) * 1000

    def test_buffer_applied_without_live(self):
        """No live price → larger (DISPLAY) buffer on the cached fare."""
        r = self._deal(min=100_000, baseline=120_000)
        apply_conservative_pricing([r])
        self.assertEqual(r["display_price"], self._buffered_round(100_000))
        self.assertTrue(r["is_deal"])
        self.assertGreater(r["discount"], 0)

    def test_anchors_on_higher_live_small_buffer(self):
        """Live above cache → anchor on live, then the smaller LIVE buffer."""
        r = self._deal(min=100_000, baseline=120_000, realtime_krw=110_000)
        apply_conservative_pricing([r])
        self.assertEqual(r["display_price"], self._buffered_round(110_000, live=True))

    def test_lower_live_ignored_but_small_buffer(self):
        """Live below cache → keep the (higher) cache as anchor, but since a live
        fare was verified, use the smaller LIVE buffer."""
        r = self._deal(min=100_000, baseline=120_000, realtime_krw=90_000)
        apply_conservative_pricing([r])
        self.assertEqual(r["display_price"], self._buffered_round(100_000, live=True))

    def test_below_threshold_stays_deal_despite_buffer(self):
        """Real fare below the threshold stays a deal even if the buffer pushes
        the displayed price back above it (buffer is display-only, not a gate)."""
        baseline = 100_000
        cutoff = baseline * (1 - DEAL_THRESHOLD_PCT / 100)
        r = self._deal(min=int(cutoff) - 2_000, baseline=baseline)  # below cutoff = deal
        apply_conservative_pricing([r])
        self.assertTrue(r["is_deal"])
        self.assertGreater(r["display_price"], cutoff)  # buffer pushed it back over

    def test_anchor_above_threshold_drops_deal(self):
        """Real fare not far enough below typical → not a deal."""
        baseline = 100_000
        cutoff = baseline * (1 - DEAL_THRESHOLD_PCT / 100)
        r = self._deal(min=int(cutoff) + 2_000, baseline=baseline)  # above cutoff
        apply_conservative_pricing([r])
        self.assertFalse(r["is_deal"])
        self.assertIn("below typical", r["price_note"])

    def test_non_deals_untouched(self):
        r = self._deal(is_deal=False)
        apply_conservative_pricing([r])
        self.assertNotIn("display_price", r)


class RequireRealtimeVerificationTests(unittest.TestCase):
    """REQUIRE_REALTIME_VERIFICATION: candidates with no confirmed live price are
    dropped (their cached fare may have sold out), EXCEPT ones whose cached fare
    is fresh (found <= FRESH_VERIFY_MAX_AGE_DAYS ago), which are kept."""

    TODAY = "2026-07-01"

    def _candidate(self, dest, link=None):
        return {
            "status": "ok", "is_deal": True, "origin": "ICN", "dest": dest,
            "trip": "oneway", "min": 100_000, "baseline": 120_000,
            "cheap": {"departure_at": "2026-07-01T08:00:00+09:00",
                      "return_at": None, "link": link},
        }

    def _run(self, candidates):
        import sys
        import types
        from unittest import mock
        import realtime as rt
        import snapshot as snap

        fake_ff = types.ModuleType("fast_flights")

        def fake_live(origin, dest, depart, ret, fx):
            return 105_000 if dest == "NRT" else None  # only NRT confirmed live

        with mock.patch.dict(sys.modules, {"fast_flights": fake_ff}), \
                mock.patch.object(rt, "usd_to_krw", return_value=1_350.0), \
                mock.patch.object(rt, "cheapest_krw", side_effect=fake_live):
            snap.crosscheck_realtime(candidates, self.TODAY)

    def test_unverified_stale_dropped_verified_kept(self):
        verified = self._candidate("NRT")                       # live price found
        unverified = self._candidate("KIX")                     # no live, no fresh cache
        self._run([verified, unverified])

        self.assertTrue(verified["is_deal"])
        self.assertEqual(verified["realtime_krw"], 105_000)
        self.assertFalse(unverified["is_deal"])
        self.assertIn("unverified", unverified.get("realtime_note", ""))

    def test_unverified_but_fresh_cache_kept(self):
        # search_date=01072026 -> cache_date 2026-07-01 == TODAY (0 days old) -> fresh
        fresh = self._candidate("KIX", link="/search/x?t=abc&search_date=01072026")
        # search_date=24062026 -> 2026-06-24 (7 days old) -> stale -> dropped
        stale = self._candidate("CTS", link="/search/y?t=def&search_date=24062026")
        self._run([fresh, stale])

        self.assertTrue(fresh["is_deal"])                # kept via freshness fallback
        self.assertIn("fresh", fresh.get("realtime_note", ""))
        self.assertFalse(stale["is_deal"])               # too old -> dropped


if __name__ == "__main__":
    unittest.main(verbosity=2)
