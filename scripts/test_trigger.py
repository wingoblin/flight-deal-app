"""
Tests for the per-user trigger.

Only the pure helpers are tested here. I/O paths (Supabase, Expo) are
mocked at the boundary — see _matches_user.

Run: python scripts/test_trigger.py
"""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trigger import _matches_user, _route_key, _is_long_haul


# Fixed "today" so tests are independent of wall clock.
TODAY = dt.date(2026, 5, 26)


def _deal(**over):
    base = {
        "from": "ICN",
        "destination": "BKK",          # 단거리 (default)
        "trip": "roundtrip",
        "price": 200_000,
        "baseline": 500_000,
        "discount_pct": 60.0,
        "airline": "KE",
        # 5일 뒤 출발 — 기본 alarm_window='30' 안에 들어옴.
        "departure_at": (TODAY + dt.timedelta(days=5)).isoformat() + "T10:00:00+09:00",
    }
    base.update(over)
    return base


def _user(**over):
    base = {
        "token": "ExponentPushToken[abc]",
        "origins": [],            # 빈 배열 = 전체 허용
        "destinations": [],       # 빈 배열 = 전체 허용
        "alarm_master": True,
        "alarm_window": "30",     # 기본값: 30일 윈도우. None 은 차단 케이스용.
        "disc_short_pct": 25,
        "disc_long_pct": 15,
        "lang": "ko",
    }
    base.update(over)
    return base


def _match(deal, user, history):
    """Wrap _matches_user with the fixed TODAY so tests are time-independent."""
    return _matches_user(deal, user, history, today=TODAY)


class RouteAndHaulTests(unittest.TestCase):

    def test_route_key_format(self):
        self.assertEqual(_route_key(_deal()), "ICN|BKK|roundtrip")

    def test_long_haul_detection(self):
        # CDG = Paris, in long-haul list
        self.assertTrue(_is_long_haul("CDG"))
        # BKK = Bangkok, not in long-haul list
        self.assertFalse(_is_long_haul("BKK"))


class MatchingTests(unittest.TestCase):

    def setUp(self):
        self.empty_history = set()

    def test_alarm_off_blocks_everything(self):
        ok, reason = _match(_deal(), _user(alarm_master=False), self.empty_history)
        self.assertFalse(ok)
        self.assertEqual(reason, "alarm_off")

    def test_origin_filter_match(self):
        u = _user(origins=["ICN", "GMP"])
        ok, _ = _match(_deal(**{"from": "ICN"}), u, self.empty_history)
        self.assertTrue(ok)

    def test_origin_filter_miss(self):
        u = _user(origins=["GMP"])
        ok, reason = _match(_deal(**{"from": "ICN"}), u, self.empty_history)
        self.assertFalse(ok)
        self.assertEqual(reason, "origin_filtered")

    def test_empty_origins_allows_all(self):
        """빈 배열 = '전체 허용'. 모든 출발지 통과해야 함."""
        u = _user(origins=[])
        ok, _ = _match(_deal(**{"from": "ICN"}), u, self.empty_history)
        self.assertTrue(ok)

    def test_destination_filter(self):
        u = _user(destinations=["FUK", "NRT"])
        ok, reason = _match(_deal(destination="BKK"), u, self.empty_history)
        self.assertFalse(ok)
        self.assertEqual(reason, "destination_filtered")

    def test_empty_destinations_allows_all(self):
        u = _user(destinations=[])
        ok, _ = _match(_deal(destination="BKK"), u, self.empty_history)
        self.assertTrue(ok)

    def test_short_haul_uses_short_cut(self):
        """단거리 deal 은 disc_short_pct 와 비교."""
        u = _user(disc_short_pct=50, disc_long_pct=10)
        # discount 40% < short cut 50% → 거부
        ok, reason = _match(_deal(destination="BKK", discount_pct=40), u, self.empty_history)
        self.assertFalse(ok)
        self.assertEqual(reason, "below_user_cut")

    def test_long_haul_uses_long_cut(self):
        """장거리 deal 은 disc_long_pct 와 비교."""
        u = _user(disc_short_pct=50, disc_long_pct=10)
        # 같은 40% 인데 장거리(CDG)면 long cut 10% 와 비교 → 통과
        ok, _ = _match(_deal(destination="CDG", discount_pct=40), u, self.empty_history)
        self.assertTrue(ok)

    def test_at_exact_cut_passes(self):
        """경계값 포함 (>=)."""
        u = _user(disc_short_pct=25)
        ok, _ = _match(_deal(discount_pct=25), u, self.empty_history)
        self.assertTrue(ok)

    def test_dedup_blocks_recent(self):
        history = {f"{_user()['token']}|{_route_key(_deal())}"}
        ok, reason = _match(_deal(), _user(), history)
        self.assertFalse(ok)
        self.assertEqual(reason, "dup_recent")

    def test_dedup_is_per_user(self):
        """A 사용자에게 보낸 게 B 사용자 dedup 에 영향 주면 안 됨."""
        history = {"ExponentPushToken[A]|ICN|BKK|roundtrip"}
        u_b = _user(token="ExponentPushToken[B]")
        ok, _ = _match(_deal(), u_b, history)
        self.assertTrue(ok)

    def test_dedup_is_per_route(self):
        """ICN-BKK 보낸 게 ICN-NRT dedup 에 영향 주면 안 됨."""
        history = {f"{_user()['token']}|ICN|BKK|roundtrip"}
        ok, _ = _match(_deal(destination="NRT"), _user(), history)
        self.assertTrue(ok)

    def test_oneway_and_roundtrip_dedup_separately(self):
        """왕복/편도는 다른 상품 → 따로 dedup."""
        history = {f"{_user()['token']}|ICN|BKK|roundtrip"}
        ok, _ = _match(_deal(trip="oneway"), _user(), history)
        self.assertTrue(ok)

    def test_all_filters_in_order(self):
        """필터 우선순위: alarm → origin → destination → cut → dedup."""
        u = _user(alarm_master=False, origins=["GMP"], disc_short_pct=99)
        ok, reason = _match(_deal(**{"from": "ICN"}), u, self.empty_history)
        self.assertFalse(ok)
        # alarm_off 가 가장 먼저 잡혀야 (early return)
        self.assertEqual(reason, "alarm_off")


class AlarmWindowTests(unittest.TestCase):
    """사양 (작업 4): user.alarm_window ∈ {'7','30',None}, 출발일까지 남은 일수 필터."""

    def setUp(self):
        self.empty_history = set()

    def _dep(self, days_from_today: int) -> str:
        d = TODAY + dt.timedelta(days=days_from_today)
        return d.isoformat() + "T10:00:00+09:00"

    def test_alarm_window_null_blocks(self):
        """alarm_window=NULL → 두 토글 모두 OFF → 차단."""
        u = _user(alarm_window=None)
        ok, reason = _match(_deal(departure_at=self._dep(3)), u, self.empty_history)
        self.assertFalse(ok)
        self.assertEqual(reason, "alarm_window_null")

    def test_no_departure_date_blocks(self):
        """deal 에 departure_at 없으면 안전하게 차단."""
        d = _deal()
        del d["departure_at"]
        ok, reason = _match(d, _user(alarm_window="7"), self.empty_history)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_departure_date")

    def test_past_departure_blocks(self):
        """이미 지난 출발은 차단."""
        u = _user(alarm_window="30")
        ok, reason = _match(_deal(departure_at=self._dep(-1)), u, self.empty_history)
        self.assertFalse(ok)
        self.assertEqual(reason, "past_departure")

    def test_window_7d_within(self):
        u = _user(alarm_window="7")
        ok, _ = _match(_deal(departure_at=self._dep(3)), u, self.empty_history)
        self.assertTrue(ok)

    def test_window_7d_outside(self):
        u = _user(alarm_window="7")
        ok, reason = _match(_deal(departure_at=self._dep(10)), u, self.empty_history)
        self.assertFalse(ok)
        self.assertEqual(reason, "outside_7d_window")

    def test_window_7d_boundary_inclusive(self):
        """D+7 은 7일 윈도우 안 (>=, not >)."""
        u = _user(alarm_window="7")
        ok, _ = _match(_deal(departure_at=self._dep(7)), u, self.empty_history)
        self.assertTrue(ok)

    def test_window_30d_within(self):
        u = _user(alarm_window="30")
        ok, _ = _match(_deal(departure_at=self._dep(25)), u, self.empty_history)
        self.assertTrue(ok)

    def test_window_30d_outside(self):
        u = _user(alarm_window="30")
        ok, reason = _match(_deal(departure_at=self._dep(45)), u, self.empty_history)
        self.assertFalse(ok)
        self.assertEqual(reason, "outside_30d_window")

    def test_window_today_passes(self):
        """오늘 출발 (D+0) 은 윈도우 안."""
        u = _user(alarm_window="7")
        ok, _ = _match(_deal(departure_at=self._dep(0)), u, self.empty_history)
        self.assertTrue(ok)

    def test_alarm_window_before_other_filters(self):
        """alarm_window_null 이 origin/dest/cut/dedup 보다 먼저 잡혀야."""
        u = _user(alarm_window=None, origins=["GMP"], disc_short_pct=99)
        ok, reason = _match(_deal(**{"from": "ICN"}), u, self.empty_history)
        self.assertFalse(ok)
        self.assertEqual(reason, "alarm_window_null")


if __name__ == "__main__":
    unittest.main(verbosity=2)
