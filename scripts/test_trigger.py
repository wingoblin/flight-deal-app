"""
Tests for the per-user trigger.

Pure helpers only. I/O paths (Supabase, Expo) are mocked at the boundary.
Bundle formatting/signature are pure and tested here too.

Run: python scripts/test_trigger.py
"""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trigger import (
    BUNDLE_TITLE,
    _matches_user,
    _route_key,
    build_bundle_body,
    bundle_signature,
    city_name,
    format_deal_line,
)
from trigger import _deal_date_token  # noqa: E402  (pure helper, tested directly)


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


def _match(deal, user):
    """Wrap _matches_user with the fixed TODAY so tests are time-independent.
    (Dedup moved out of _matches_user to the bundle level — see BundleSignatureTests.)"""
    return _matches_user(deal, user, today=TODAY)


def _bdeal(frm="ICN", dest="MNL", price=200_000, trip="roundtrip",
           dep="2026-05-28", ret="2026-06-03"):
    """Deal fixture for bundle formatting/signature tests."""
    return {
        "from": frm,
        "destination": dest,
        "trip": trip,
        "price": price,
        "departure_at": f"{dep}T10:00:00+09:00",
        "return_at": (f"{ret}T18:00:00+09:00" if trip == "roundtrip" and ret else None),
    }


class RouteKeyTests(unittest.TestCase):

    def test_route_key_format(self):
        self.assertEqual(_route_key(_deal()), "ICN|BKK|roundtrip")


class MatchingTests(unittest.TestCase):

    def test_alarm_off_blocks_everything(self):
        ok, reason = _match(_deal(), _user(alarm_master=False))
        self.assertFalse(ok)
        self.assertEqual(reason, "alarm_off")

    def test_origin_filter_match(self):
        u = _user(origins=["ICN", "GMP"])
        ok, _ = _match(_deal(**{"from": "ICN"}), u)
        self.assertTrue(ok)

    def test_origin_filter_miss(self):
        u = _user(origins=["GMP"])
        ok, reason = _match(_deal(**{"from": "ICN"}), u)
        self.assertFalse(ok)
        self.assertEqual(reason, "origin_filtered")

    def test_empty_origins_allows_all(self):
        """빈 배열 = '전체 허용'. 모든 출발지 통과해야 함."""
        u = _user(origins=[])
        ok, _ = _match(_deal(**{"from": "ICN"}), u)
        self.assertTrue(ok)

    def test_destination_filter(self):
        u = _user(destinations=["FUK", "NRT"])
        ok, reason = _match(_deal(destination="BKK"), u)
        self.assertFalse(ok)
        self.assertEqual(reason, "destination_filtered")

    def test_empty_destinations_allows_all(self):
        u = _user(destinations=[])
        ok, _ = _match(_deal(destination="BKK"), u)
        self.assertTrue(ok)

    def test_low_discount_deal_still_passes(self):
        """Step 3: trigger 의 per-user discount cut 제거됨. snapshot 이 flag 한
        deal 은 discount_pct 가 낮아도(또는 음수여도) trigger 를 통과해야 함
        (route/window 만 적용; dedup 은 묶음 레벨)."""
        u = _user()
        ok, _ = _match(_deal(discount_pct=2.0), u)
        self.assertTrue(ok)
        ok_neg, _ = _match(_deal(discount_pct=-3.0), u)
        self.assertTrue(ok_neg)

    def test_all_filters_in_order(self):
        """필터 우선순위: alarm → window → origin → destination."""
        u = _user(alarm_master=False, origins=["GMP"])
        ok, reason = _match(_deal(**{"from": "ICN"}), u)
        self.assertFalse(ok)
        # alarm_off 가 가장 먼저 잡혀야 (early return)
        self.assertEqual(reason, "alarm_off")


class AlarmWindowTests(unittest.TestCase):
    """사양 (확정): alarm_window ∈ {'7','30',None}.
    - 출발일 검증 (존재 + 미래) 은 무조건 적용
    - '7'/'30' 은 days_until_departure 의 상한
    - NULL 은 상한 없음 (UI 의 7d/30d 토글 둘 다 OFF) — 미래 유효 딜은 다 통과"""

    def _dep(self, days_from_today: int) -> str:
        d = TODAY + dt.timedelta(days=days_from_today)
        return d.isoformat() + "T10:00:00+09:00"

    def test_no_departure_date_blocks(self):
        """deal 에 departure_at 없으면 안전하게 차단."""
        d = _deal()
        del d["departure_at"]
        ok, reason = _match(d, _user(alarm_window="7"))
        self.assertFalse(ok)
        self.assertEqual(reason, "no_departure_date")

    def test_past_departure_blocks(self):
        """이미 지난 출발은 차단."""
        u = _user(alarm_window="30")
        ok, reason = _match(_deal(departure_at=self._dep(-1)), u)
        self.assertFalse(ok)
        self.assertEqual(reason, "past_departure")

    def test_window_7d_within(self):
        u = _user(alarm_window="7")
        ok, _ = _match(_deal(departure_at=self._dep(3)), u)
        self.assertTrue(ok)

    def test_window_7d_outside(self):
        u = _user(alarm_window="7")
        ok, reason = _match(_deal(departure_at=self._dep(10)), u)
        self.assertFalse(ok)
        self.assertEqual(reason, "outside_7d_window")

    def test_window_7d_boundary_inclusive(self):
        """D+7 은 7일 윈도우 안 (>=, not >)."""
        u = _user(alarm_window="7")
        ok, _ = _match(_deal(departure_at=self._dep(7)), u)
        self.assertTrue(ok)

    def test_window_30d_within(self):
        u = _user(alarm_window="30")
        ok, _ = _match(_deal(departure_at=self._dep(25)), u)
        self.assertTrue(ok)

    def test_window_30d_outside(self):
        u = _user(alarm_window="30")
        ok, reason = _match(_deal(departure_at=self._dep(45)), u)
        self.assertFalse(ok)
        self.assertEqual(reason, "outside_30d_window")

    def test_window_today_passes(self):
        """오늘 출발 (D+0) 은 윈도우 안."""
        u = _user(alarm_window="7")
        ok, _ = _match(_deal(departure_at=self._dep(0)), u)
        self.assertTrue(ok)

    def test_alarm_window_invalid_value_blocks(self):
        """사양에 없는 값 ('abc' 등) 은 추측하지 않고 차단."""
        u = _user(alarm_window="abc")
        ok, reason = _match(_deal(departure_at=self._dep(3)), u)
        self.assertFalse(ok)
        self.assertEqual(reason, "alarm_window_invalid")

    def test_alarm_window_null_passes_future_departure(self):
        """NULL = 상한 없음. 정상 미래 출발일 통과."""
        u = _user(alarm_window=None)
        ok, _ = _match(_deal(departure_at=self._dep(15)), u)
        self.assertTrue(ok)

    def test_alarm_window_null_far_future_passes(self):
        """NULL 이면 D+200 같은 먼 미래도 통과 (상한 없음)."""
        u = _user(alarm_window=None)
        ok, _ = _match(_deal(departure_at=self._dep(200)), u)
        self.assertTrue(ok)

    def test_alarm_window_null_still_blocks_past_departure(self):
        """NULL 이어도 과거 출발은 차단 — stale cache 거짓 양성 방지."""
        u = _user(alarm_window=None)
        ok, reason = _match(_deal(departure_at=self._dep(-1)), u)
        self.assertFalse(ok)
        self.assertEqual(reason, "past_departure")

    def test_alarm_window_null_still_blocks_missing_date(self):
        """NULL 이어도 출발일 누락은 차단 — 정책 일관성 + 사용자 행동 가능성."""
        d = _deal()
        del d["departure_at"]
        u = _user(alarm_window=None)
        ok, reason = _match(d, u)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_departure_date")

    def test_alarm_window_null_still_subject_to_other_filters(self):
        """NULL 은 윈도 상한만 우회. origin/destination 필터는 적용."""
        u = _user(alarm_window=None, origins=["GMP"])
        ok, reason = _match(_deal(**{"from": "ICN"}), u)
        self.assertFalse(ok)
        self.assertEqual(reason, "origin_filtered")


class CityNameTests(unittest.TestCase):

    def test_known_codes(self):
        self.assertEqual(city_name("ICN"), "인천")
        self.assertEqual(city_name("MNL"), "마닐라")
        self.assertEqual(city_name("DAD"), "다낭")
        self.assertEqual(city_name("BKK"), "방콕")

    def test_unknown_code_falls_back_to_code(self):
        # No mapping → return the code itself (warning goes to stderr).
        self.assertEqual(city_name("ZZZ"), "ZZZ")


class DateTokenTests(unittest.TestCase):

    def test_roundtrip_range(self):
        d = _bdeal(dep="2026-05-28", ret="2026-06-03")
        self.assertEqual(_deal_date_token(d), "[5.28~6.03]")

    def test_oneway_has_label_before_bracket(self):
        d = _bdeal(trip="oneway", dep="2026-06-03")
        self.assertEqual(_deal_date_token(d), "편도 [6.03]")

    def test_month_unpadded_day_padded(self):
        d = _bdeal(trip="oneway", dep="2026-06-03")
        # month 6 -> "6" (no pad), day 3 -> "03" (padded)
        self.assertEqual(_deal_date_token(d), "편도 [6.03]")


class DealLineTests(unittest.TestCase):

    def test_roundtrip_line(self):
        d = _bdeal(frm="ICN", dest="MNL", price=188_000, dep="2026-05-28", ret="2026-06-03")
        self.assertEqual(format_deal_line(d), "인천→마닐라 188,000원 [5.28~6.03]")

    def test_oneway_line(self):
        d = _bdeal(frm="ICN", dest="BKK", price=245_000, trip="oneway", dep="2026-06-03")
        self.assertEqual(format_deal_line(d), "인천→방콕 245,000원 편도 [6.03]")

    def test_unmapped_code_in_line(self):
        d = _bdeal(frm="ICN", dest="ZZZ", price=100_000, trip="oneway", dep="2026-06-03")
        self.assertEqual(format_deal_line(d), "인천→ZZZ 100,000원 편도 [6.03]")

    def test_price_has_thousands_comma_exact_integer(self):
        d = _bdeal(price=1_234_567, trip="oneway", dep="2026-06-03")
        self.assertIn("1,234,567원", format_deal_line(d))


class BundleBodyTests(unittest.TestCase):

    def test_zero_deals(self):
        self.assertEqual(build_bundle_body([]), "")

    def test_one_deal_no_extra(self):
        body = build_bundle_body([_bdeal(dest="MNL", price=188_000)])
        self.assertEqual(body, "인천→마닐라 188,000원 [5.28~6.03]")
        self.assertNotIn("외", body)

    def test_three_deals_no_extra(self):
        deals = [
            _bdeal(dest="MNL", price=188_000),
            _bdeal(dest="DAD", price=211_000),
            _bdeal(dest="BKK", price=245_000),
        ]
        body = build_bundle_body(deals)
        self.assertEqual(len(body.split("\n")), 3)
        self.assertNotIn("외", body)

    def test_four_deals_collapse_one(self):
        deals = [_bdeal(dest=c, price=p) for c, p in
                 [("MNL", 188_000), ("DAD", 211_000), ("BKK", 245_000), ("OSA", 300_000)]]
        body = build_bundle_body(deals)
        lines = body.split("\n")
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[-1].endswith("외 1건"))

    def test_sorted_cheapest_first(self):
        deals = [
            _bdeal(dest="BKK", price=245_000),
            _bdeal(dest="MNL", price=188_000),
            _bdeal(dest="DAD", price=211_000),
        ]
        lines = build_bundle_body(deals).split("\n")
        self.assertTrue(lines[0].startswith("인천→마닐라"))
        self.assertTrue(lines[1].startswith("인천→다낭"))
        self.assertTrue(lines[2].startswith("인천→방콕"))

    def test_mixed_oneway_roundtrip(self):
        deals = [
            _bdeal(dest="MNL", price=188_000, dep="2026-05-28", ret="2026-06-03"),
            _bdeal(dest="DAD", price=211_000, dep="2026-06-01", ret="2026-06-07"),
            _bdeal(dest="BKK", price=245_000, trip="oneway", dep="2026-06-03"),
        ]
        body = build_bundle_body(deals)
        self.assertEqual(
            body,
            "인천→마닐라 188,000원 [5.28~6.03]\n"
            "인천→다낭 211,000원 [6.01~6.07]\n"
            "인천→방콕 245,000원 편도 [6.03]",
        )

    def test_mockup_25_deals_exact(self):
        """Reproduce the approved mockup body exactly (top 3 + 외 22건)."""
        deals = [
            _bdeal(dest="MNL", price=188_000, dep="2026-05-28", ret="2026-06-03"),
            _bdeal(dest="DAD", price=211_000, dep="2026-06-01", ret="2026-06-07"),
            _bdeal(dest="BKK", price=245_000, trip="oneway", dep="2026-06-03"),
        ]
        # 22 filler deals, all pricier so they stay out of the top 3.
        deals += [_bdeal(dest="OSA", price=400_000 + i) for i in range(22)]
        body = build_bundle_body(deals)
        self.assertEqual(
            body,
            "인천→마닐라 188,000원 [5.28~6.03]\n"
            "인천→다낭 211,000원 [6.01~6.07]\n"
            "인천→방콕 245,000원 편도 [6.03] 외 22건",
        )

    def test_missing_city_does_not_crash(self):
        body = build_bundle_body([_bdeal(dest="ZZZ", price=99_000, trip="oneway", dep="2026-06-03")])
        self.assertEqual(body, "인천→ZZZ 99,000원 편도 [6.03]")


class BundleSignatureTests(unittest.TestCase):
    """Dedup identity: top-N route set + total count, ignoring price/date wobble."""

    def _three(self):
        return [
            _bdeal(dest="MNL", price=188_000),
            _bdeal(dest="DAD", price=211_000),
            _bdeal(dest="BKK", price=245_000),
        ]

    def test_price_wobble_keeps_signature(self):
        a = self._three()
        b = self._three()
        for d in b:
            d["price"] += 1_500   # cache refresh nudges every price
        self.assertEqual(bundle_signature(a), bundle_signature(b))

    def test_date_change_keeps_signature(self):
        a = self._three()
        b = self._three()
        for d in b:
            d["departure_at"] = "2026-07-01T10:00:00+09:00"
        self.assertEqual(bundle_signature(a), bundle_signature(b))

    def test_input_order_independent(self):
        a = self._three()
        b = list(reversed(self._three()))
        self.assertEqual(bundle_signature(a), bundle_signature(b))

    def test_new_top_route_changes_signature(self):
        a = self._three()
        b = self._three()
        # A cheaper new route displaces BKK from the top 3.
        b.append(_bdeal(dest="OSA", price=100_000))
        # Same count change AND top-set change → must differ.
        self.assertNotEqual(bundle_signature(a), bundle_signature(b))

    def test_count_change_changes_signature(self):
        a = self._three()
        b = self._three()
        # Add a pricier route: top-3 set unchanged, but total count changes.
        b.append(_bdeal(dest="OSA", price=999_000))
        self.assertNotEqual(bundle_signature(a), bundle_signature(b))

    def test_different_top_set_same_count_changes_signature(self):
        a = self._three()
        b = [
            _bdeal(dest="MNL", price=188_000),
            _bdeal(dest="DAD", price=211_000),
            _bdeal(dest="OSA", price=245_000),   # OSA instead of BKK, same count
        ]
        self.assertNotEqual(bundle_signature(a), bundle_signature(b))


class TitleTests(unittest.TestCase):

    def test_bundle_title_matches_mockup(self):
        self.assertEqual(BUNDLE_TITLE, "항공권 정보가 갱신되었습니다.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
