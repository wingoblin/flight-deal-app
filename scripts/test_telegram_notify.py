"""Tests for the Telegram alert selection (two-lane select_deals()).

Pure functions only — no network, no Telegram, no playwright. Covers the cheap
(absolute-price) lane, the original discount lane, and the dedupe keys that keep
a permanently-cheap route from re-alerting all day.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from telegram_notify import deal_key, interleave, route_key, select_deals

TODAY = "2026-08-01"
NOW = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.timezone.utc)


def deal(dest, price, disc, trip="roundtrip", origin="ICN", dep="2026-09-10",
         ret="2026-09-15", airline="XX"):
    d = {
        "from": origin,
        "destination": dest,
        "trip": trip,
        "price": price,
        "discount_pct": disc,
        "departure_at": f"{dep}T09:00:00+09:00",
        "airline": airline,
    }
    if trip == "roundtrip":
        d["return_at"] = f"{ret}T09:00:00+09:00"
    return d


def select(deals, state=None, cheap_price=180000, min_discount=25, max_cards=6,
           window_hours=24, max_price=500000, price_override_discount=35,
           rank="discount"):
    return select_deals(
        deals, state or {"sent": {}}, NOW, min_discount, max_cards, window_hours,
        rank, max_price=max_price, price_override_discount=price_override_discount,
        cheap_price=cheap_price,
    )


class CheapLaneTests(unittest.TestCase):
    """Absolute-price lane: a round-trip <= cheap_price alerts on price alone."""

    def test_cheap_roundtrip_below_min_discount_is_selected(self):
        # The exact case that was silently dropped in production: a genuinely
        # cheap round-trip whose discount is far below the 25% gate.
        d = deal("OSA", 152000, 12.0, origin="PUS")
        self.assertEqual(select([d]), [d])

    def test_cheap_fare_with_negative_discount_is_selected(self):
        # Priced above its own baseline but cheap in absolute terms -> still alerts.
        d = deal("FUK", 167000, -0.7)
        self.assertEqual(select([d]), [d])

    def test_price_at_the_boundary_is_inclusive(self):
        d = deal("PQC", 180000, 0)
        self.assertEqual(select([d]), [d])

    def test_just_over_the_boundary_falls_to_discount_lane(self):
        # 180,001: too expensive for the cheap lane, so the discount gate
        # applies again and 0% doesn't clear it.
        self.assertEqual(select([deal("PQC", 180001, 0)]), [])
        d = deal("PQC", 180001, 26.0)
        self.assertEqual(select([d]), [d])

    def test_cheap_lane_ranks_by_price(self):
        a, b, c = deal("OSA", 90000, 0), deal("TYO", 64000, 0), deal("FUK", 120000, 0)
        self.assertEqual(select([a, b, c]), [b, a, c])

    def test_disabled_when_cheap_price_is_none(self):
        # Original behaviour must be recoverable by unsetting the env var.
        self.assertEqual(select([deal("OSA", 54000, 11.5)], cheap_price=None), [])


class RoundTripOnlyTests(unittest.TestCase):
    """One-way fares are never alerted on, in either lane, at any price."""

    def test_cheap_oneway_is_rejected(self):
        self.assertEqual(select([deal("OSA", 54000, 11.5, trip="oneway", origin="PUS")]), [])

    def test_very_cheap_oneway_is_rejected(self):
        self.assertEqual(select([deal("TYO", 10000, 90.0, trip="oneway")]), [])

    def test_expensive_oneway_is_rejected(self):
        self.assertEqual(select([deal("MEL", 947000, 36.8, trip="oneway")]), [])

    def test_oneway_does_not_consume_a_card_slot(self):
        oneway = [deal(f"O{i}", 50000, 0, trip="oneway") for i in range(10)]
        rt = deal("OSA", 152000, 12.0, origin="PUS")
        self.assertEqual(select(oneway + [rt]), [rt])


class DiscountLaneTests(unittest.TestCase):
    """The original rule must survive unchanged."""

    def test_roundtrip_above_min_discount_is_selected(self):
        d = deal("SGN", 262000, 25.9)
        self.assertEqual(select([d]), [d])

    def test_roundtrip_below_min_discount_is_rejected(self):
        self.assertEqual(select([deal("HKG", 313000, 24.9)]), [])

    def test_price_ceiling_blocks_expensive_moderate_discount(self):
        self.assertEqual(select([deal("MEL", 947000, 30.0)]), [])

    def test_big_discount_overrides_price_ceiling(self):
        d = deal("MEL", 947000, 36.8)
        self.assertEqual(select([d]), [d])


class LaneInteractionTests(unittest.TestCase):

    def test_cheap_lane_does_not_starve_discount_lane(self):
        cheap = [deal(f"C{i}", 150000 + i, 0) for i in range(10)]
        pricey = [deal("SGN", 262000, 25.9), deal("MEL", 947000, 36.8)]
        picked = select(cheap + pricey, max_cards=6)
        self.assertEqual(len(picked), 6)
        # Alternating lanes -> both discount deals survive alongside cheap ones.
        self.assertIn(pricey[0], picked)
        self.assertIn(pricey[1], picked)

    def test_one_lane_absorbs_remainder_when_other_is_empty(self):
        cheap = [deal(f"C{i}", 150000 + i, 0) for i in range(10)]
        self.assertEqual(len(select(cheap, max_cards=6)), 6)

    def test_cheap_deal_is_not_double_counted_in_both_lanes(self):
        # Cheap AND a big discount: must appear exactly once.
        d = deal("OSA", 150000, 40.0)
        self.assertEqual(select([d]), [d])

    def test_interleave_preserves_order_within_each_lane(self):
        self.assertEqual(interleave([1, 3, 5], [2, 4], 5), [1, 2, 3, 4, 5])

    def test_interleave_respects_limit(self):
        self.assertEqual(interleave([1, 2, 3], [4, 5, 6], 3), [1, 4, 2])


class DedupeTests(unittest.TestCase):

    def _sent(self, key, hours_ago):
        return {"sent": {key: (NOW - dt.timedelta(hours=hours_ago)).isoformat()}}

    def test_same_deal_within_window_is_skipped(self):
        d = deal("OSA", 54000, 0)
        self.assertEqual(select([d], state=self._sent(deal_key(d), 1)), [])

    def test_same_deal_outside_window_is_resent(self):
        d = deal("OSA", 54000, 0)
        self.assertEqual(select([d], state=self._sent(deal_key(d), 25)), [d])

    def test_cheap_route_with_a_new_departure_date_is_suppressed(self):
        # The spam case: 오사카 is under the floor every day and offers a fresh
        # departure date each snapshot, so deal_key alone would re-alert it all
        # day. route_key must catch it.
        sent = deal("OSA", 54000, 0, dep="2026-09-10")
        fresh = deal("OSA", 53000, 0, dep="2026-09-11")
        self.assertEqual(select([fresh], state=self._sent(route_key(sent), 1)), [])

    def test_cheap_route_is_alertable_again_after_the_window(self):
        sent = deal("OSA", 54000, 0, dep="2026-09-10")
        fresh = deal("OSA", 53000, 0, dep="2026-09-11")
        self.assertEqual(select([fresh], state=self._sent(route_key(sent), 25)), [fresh])

    def test_a_different_origin_is_a_different_route(self):
        icn = deal("OSA", 58000, 0, origin="ICN")
        pus = deal("OSA", 54000, 0, origin="PUS")
        self.assertEqual(select([pus], state=self._sent(route_key(icn), 1)), [pus])

    def test_discount_lane_keeps_per_departure_date_behaviour(self):
        # route_key is written on send but only *checked* by the cheap lane, so a
        # round-trip deal on a new date still alerts.
        sent = deal("SGN", 262000, 25.9, trip="roundtrip", dep="2026-09-10")
        fresh = deal("SGN", 262000, 25.9, trip="roundtrip", dep="2026-10-20")
        self.assertEqual(select([fresh], state=self._sent(route_key(sent), 1)), [fresh])


class MalformedInputTests(unittest.TestCase):

    def test_already_departed_deal_is_skipped(self):
        self.assertEqual(select([deal("OSA", 54000, 0, dep="2026-07-30")]), [])

    def test_missing_price_is_skipped(self):
        d = deal("OSA", 54000, 0)
        del d["price"]
        self.assertEqual(select([d]), [])

    def test_unparseable_departure_is_skipped(self):
        d = deal("OSA", 54000, 0)
        d["departure_at"] = "not-a-date"
        self.assertEqual(select([d]), [])

    def test_missing_discount_defaults_to_zero_and_still_alerts_when_cheap(self):
        d = deal("OSA", 54000, 0)
        del d["discount_pct"]
        self.assertEqual(select([d]), [d])


if __name__ == "__main__":
    os.environ.setdefault("TELEGRAM_NOW", TODAY)
    unittest.main(verbosity=2)
