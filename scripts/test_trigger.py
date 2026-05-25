"""
Unit tests for trigger.py.

Two layers:
1. Pure-logic tests with hand-crafted dicts — verify each branch.
2. Backtest against the real SQLite snapshots — verify the configured
   thresholds produce a sane volume of notifications.

Run: python scripts/test_trigger.py
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import sys
import unittest
from pathlib import Path

# Make scripts/ importable when running this file directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from trigger import evaluate_deals, format_notification


def _deal(**overrides):
    """Builder for test deal dicts — fill in sensible defaults, override per-test."""
    base = {
        "from": "ICN",
        "destination": "BKK",
        "trip": "roundtrip",
        "price": 200_000,
        "baseline": 500_000,
        "discount_pct": 60.0,
        "departure_at": "2026-08-01T10:00:00+09:00",
        "return_at": "2026-08-08T15:00:00+07:00",
        "transfers": 0,
        "airline": "KE",
        "gate": "Trip.com",
        "n": 30,
    }
    base.update(overrides)
    return base


NOW = dt.datetime(2026, 5, 24, 12, 0, tzinfo=dt.timezone.utc)


class PureLogicTests(unittest.TestCase):

    def test_passes_at_exact_threshold(self):
        """60% deal should fire when cut is 60% — boundary inclusive."""
        decisions = evaluate_deals([_deal(discount_pct=60.0)], {}, NOW)
        self.assertTrue(decisions[0].should_notify)
        self.assertEqual(decisions[0].reason, "ok")

    def test_below_cut_dropped(self):
        decisions = evaluate_deals([_deal(discount_pct=59.9)], {}, NOW)
        self.assertFalse(decisions[0].should_notify)
        self.assertEqual(decisions[0].reason, "below_cut")

    def test_low_n_dropped(self):
        """High discount but tiny sample = untrustworthy baseline → drop."""
        decisions = evaluate_deals([_deal(discount_pct=85.0, n=2)], {}, NOW)
        self.assertFalse(decisions[0].should_notify)
        self.assertEqual(decisions[0].reason, "low_n")

    def test_missing_n_passes_n_gate(self):
        """deals.json doesn't carry n today — absent n must not block firing
        (otherwise the gate silently mutes everything in prod)."""
        deal = _deal(discount_pct=70.0)
        deal.pop("n")
        decisions = evaluate_deals([deal], {}, NOW)
        self.assertTrue(decisions[0].should_notify)

    def test_dedup_suppresses_recent(self):
        """Same route pushed 3 days ago → suppress."""
        recent = (NOW - dt.timedelta(days=3)).isoformat()
        history = {"ICN|BKK|roundtrip": recent}
        decisions = evaluate_deals([_deal()], history, NOW)
        self.assertFalse(decisions[0].should_notify)
        self.assertEqual(decisions[0].reason, "dup_recent")

    def test_dedup_lets_old_push_through(self):
        """Same route pushed 8 days ago → fire (past dedup window)."""
        old = (NOW - dt.timedelta(days=8)).isoformat()
        history = {"ICN|BKK|roundtrip": old}
        decisions = evaluate_deals([_deal()], history, NOW)
        self.assertTrue(decisions[0].should_notify)

    def test_dedup_keys_on_trip_type(self):
        """ICN-BKK roundtrip and ICN-BKK oneway are different products."""
        history = {"ICN|BKK|roundtrip": NOW.isoformat()}
        oneway = _deal(trip="oneway", return_at=None)
        decisions = evaluate_deals([oneway], history, NOW)
        self.assertTrue(decisions[0].should_notify)

    def test_diagnostics_always_populated(self):
        """Every decision must carry full diagnostics for post-hoc tuning."""
        decisions = evaluate_deals(
            [_deal(discount_pct=30), _deal(discount_pct=70), _deal(discount_pct=80, n=2)],
            {}, NOW,
        )
        for d in decisions:
            self.assertIn("discount_pct", d.diagnostics)
            self.assertIn("threshold_pct", d.diagnostics)
            self.assertIn("route", d.diagnostics)

    def test_threshold_kwarg_overrides_config(self):
        """Backtesting needs to sweep thresholds without reimporting config."""
        deal = _deal(discount_pct=45.0)
        # At default 60% cut: dropped.
        self.assertFalse(evaluate_deals([deal], {}, NOW)[0].should_notify)
        # Sweep down to 40%: passes.
        self.assertTrue(evaluate_deals([deal], {}, NOW, threshold_pct=40.0)[0].should_notify)

    def test_format_notification_uses_man_unit(self):
        """Push body shows price in 만원 — Korean users read it instantly."""
        title, body = format_notification(_deal(price=523_400, discount_pct=70))
        self.assertIn("70%", title)
        self.assertIn("52만원", body)


class BacktestAgainstRealData(unittest.TestCase):
    """Run trigger over actual SQLite snapshots — verify notification volume
    matches what we expect (≥60%, n≥5, dedup 7d should yield a handful per day)."""

    DB = Path(__file__).resolve().parent.parent / "data" / "flight_deals.db"

    def setUp(self):
        if not self.DB.exists():
            self.skipTest(f"Backtest DB not present at {self.DB}")

    def test_volume_with_real_snapshots(self):
        """Simulate running trigger on each day's deals, with rolling 7d dedup."""
        conn = sqlite3.connect(self.DB)
        cur = conn.cursor()

        # Build pseudo-deals from snapshots: each row = a route's cheapest that day.
        # We use route-wide AVG(median) as baseline (proxy for what deals.json's
        # baseline would look like once enough history exists).
        cur.execute("""
            WITH route_baseline AS (
                SELECT origin, destination, trip,
                       AVG(median) AS baseline,
                       COUNT(*) AS days_seen
                FROM snapshots
                GROUP BY origin, destination, trip
            )
            SELECT s.snapshot_date, s.origin, s.destination, s.trip,
                   s.min_price, ROUND(r.baseline) AS baseline,
                   ROUND(100.0 * (r.baseline - s.min_price) / r.baseline, 1) AS disc_pct,
                   s.n
            FROM snapshots s
            JOIN route_baseline r USING (origin, destination, trip)
            WHERE r.baseline > 0 AND r.days_seen >= 3
            ORDER BY s.snapshot_date
        """)
        rows = cur.fetchall()
        conn.close()

        # Group by day, simulate sequential evaluation with growing history.
        sent_history: dict = {}
        per_day_fires: dict = {}
        for date, origin, dest, trip, price, baseline, disc, n in rows:
            deal = {
                "from": origin, "destination": dest, "trip": trip,
                "price": price, "baseline": baseline,
                "discount_pct": disc, "n": n, "airline": "",
            }
            day_dt = dt.datetime.fromisoformat(date).replace(tzinfo=dt.timezone.utc)
            decisions = evaluate_deals([deal], sent_history, day_dt)
            if decisions[0].should_notify:
                per_day_fires.setdefault(date, 0)
                per_day_fires[date] += 1
                sent_history["|".join([origin, dest, trip])] = day_dt.isoformat()

        total = sum(per_day_fires.values())
        days = len({date for date, *_ in [(r[0],) for r in rows]})
        print(f"\n[backtest] {total} pushes over {days} days; per-day: {per_day_fires}")

        # Sanity bounds — not exact assertions, just guardrails. If the
        # configured thresholds suddenly produce 0 or 100+ per day, something
        # is wrong with either the data or the config.
        self.assertGreater(total, 0, "Configured thresholds emit nothing — too strict?")
        if days:
            avg = total / days
            self.assertLess(avg, 20, f"Too many pushes/day ({avg:.1f}) — cut too low?")


if __name__ == "__main__":
    unittest.main(verbosity=2)
