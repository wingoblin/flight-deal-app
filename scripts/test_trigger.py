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

from trigger import _matches_user, _route_key


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
        "lang": "ko",
    }
    base.update(over)
    return base


def _match(deal, user, history):
    """Wrap _matches_user with the fixed TODAY so tests are time-independent."""
    return _matches_user(deal, user, history, today=TODAY)


class RouteKeyTests(unittest.TestCase):

    def test_route_key_format(self):
        self.assertEqual(_route_key(_deal()), "ICN|BKK|roundtrip")


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

    def test_low_discount_deal_still_passes(self):
        """Step 3: trigger 의 per-user discount cut 제거됨. snapshot 이 flag 한
        deal 은 discount_pct 가 낮아도(또는 음수여도) trigger 를 통과해야 함
        (route/window/dedup 만 적용)."""
        u = _user()
        ok, _ = _match(_deal(discount_pct=2.0), u, self.empty_history)
        self.assertTrue(ok)
        ok_neg, _ = _match(_deal(discount_pct=-3.0), u, self.empty_history)
        self.assertTrue(ok_neg)

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
        """필터 우선순위: alarm → window → origin → destination → dedup."""
        u = _user(alarm_master=False, origins=["GMP"])
        ok, reason = _match(_deal(**{"from": "ICN"}), u, self.empty_history)
        self.assertFalse(ok)
        # alarm_off 가 가장 먼저 잡혀야 (early return)
        self.assertEqual(reason, "alarm_off")


class AlarmWindowTests(unittest.TestCase):
    """사양 (확정): alarm_window ∈ {'7','30',None}.
    - 출발일 검증 (존재 + 미래) 은 무조건 적용
    - '7'/'30' 은 days_until_departure 의 상한
    - NULL 은 상한 없음 (UI 의 7d/30d 토글 둘 다 OFF) — 미래 유효 딜은 다 통과"""

    def setUp(self):
        self.empty_history = set()

    def _dep(self, days_from_today: int) -> str:
        d = TODAY + dt.timedelta(days=days_from_today)
        return d.isoformat() + "T10:00:00+09:00"

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

    def test_alarm_window_invalid_value_blocks(self):
        """사양에 없는 값 ('abc' 등) 은 추측하지 않고 차단."""
        u = _user(alarm_window="abc")
        ok, reason = _match(_deal(departure_at=self._dep(3)), u, self.empty_history)
        self.assertFalse(ok)
        self.assertEqual(reason, "alarm_window_invalid")

    def test_alarm_window_null_passes_future_departure(self):
        """NULL = 상한 없음. 정상 미래 출발일 통과."""
        u = _user(alarm_window=None)
        ok, _ = _match(_deal(departure_at=self._dep(15)), u, self.empty_history)
        self.assertTrue(ok)

    def test_alarm_window_null_far_future_passes(self):
        """NULL 이면 D+200 같은 먼 미래도 통과 (상한 없음)."""
        u = _user(alarm_window=None)
        ok, _ = _match(_deal(departure_at=self._dep(200)), u, self.empty_history)
        self.assertTrue(ok)

    def test_alarm_window_null_still_blocks_past_departure(self):
        """NULL 이어도 과거 출발은 차단 — stale cache 거짓 양성 방지."""
        u = _user(alarm_window=None)
        ok, reason = _match(_deal(departure_at=self._dep(-1)), u, self.empty_history)
        self.assertFalse(ok)
        self.assertEqual(reason, "past_departure")

    def test_alarm_window_null_still_blocks_missing_date(self):
        """NULL 이어도 출발일 누락은 차단 — 정책 일관성 + 사용자 행동 가능성."""
        d = _deal()
        del d["departure_at"]
        u = _user(alarm_window=None)
        ok, reason = _match(d, u, self.empty_history)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_departure_date")

    def test_alarm_window_null_still_subject_to_other_filters(self):
        """NULL 은 윈도 상한만 우회. origin/destination/cut/dedup 필터는 적용."""
        u = _user(alarm_window=None, origins=["GMP"])
        ok, reason = _match(_deal(**{"from": "ICN"}), u, self.empty_history)
        self.assertFalse(ok)
        self.assertEqual(reason, "origin_filtered")


if __name__ == "__main__":
    unittest.main(verbosity=2)
