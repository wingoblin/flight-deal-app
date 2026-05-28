"""
Per-user push notification trigger (bundled).

Design:
- Pull all active subscribers from Supabase.
- For each user, evaluate every deal against the user's route/window filters.
- Send ONE bundled notification per user (cheapest matches first) rather than
  one push per deal — so a user with 25 matches gets a single notification.
- Per-user 3-day dedup keyed on (token, bundle signature).

Idempotency / safety:
- Sent-history is checked per (token, signature) so the 30-min cron doesn't
  re-send an unchanged bundle. The signature ignores price/date wobble.
- Failures on one user don't abort the run for others.
- DRY_RUN env var lets you simulate without sending.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Iterable

from config import (
    PUSH_DEDUP_DAYS,
    PUSH_HISTORY_RETENTION_DAYS,
)


# ---------- Pure helpers ----------

def _route_key(deal: dict) -> str:
    """Identity for dedup. Round-trip and one-way to the same city are
    different products, so trip is part of the key."""
    return f"{deal['from']}|{deal['destination']}|{deal['trip']}"


# Bundled notification: one push per user summarizing their cheapest matches,
# instead of one push per deal (which could fire 25 at once).
BUNDLE_TITLE = "항공권 정보가 갱신되었습니다."
BUNDLE_TOP_N = 3   # lines shown in the body; the rest collapse into "외 N건"


# Airport code → Korean city name for notification bodies. Covers
# config.ORIGINS + DESTINATIONS. Missing codes fall back to the raw code
# (with a stderr warning) so an unmapped route never breaks a push.
CITY_NAMES = {
    # 출발지
    "ICN": "인천", "GMP": "김포", "PUS": "부산", "TAE": "대구", "CJU": "제주",
    # 일본
    "TYO": "도쿄", "OSA": "오사카", "FUK": "후쿠오카", "NGO": "나고야", "OKA": "오키나와",
    "KMQ": "고마쓰", "TAK": "다카마쓰", "KOJ": "가고시마", "KMJ": "구마모토",
    "MYJ": "마쓰야마", "HIJ": "히로시마", "FSZ": "시즈오카",
    # 동남아
    "BKK": "방콕", "DAD": "다낭", "HAN": "하노이", "SGN": "호치민", "CEB": "세부",
    "MNL": "마닐라", "SIN": "싱가포르", "KUL": "쿠알라룸푸르", "CGK": "자카르타",
    "DPS": "발리", "KLO": "칼리보", "USM": "코사무이", "HKT": "푸껫", "KBV": "끄라비",
    "SAI": "시엠립", "CNX": "치앙마이", "CXR": "나트랑", "PQC": "푸꾸옥", "VTE": "비엔티안",
    "BKI": "코타키나발루",
    # 중화권
    "TPE": "타이베이", "HKG": "홍콩",
    # 유럽
    "CDG": "파리", "LHR": "런던", "FCO": "로마", "BCN": "바르셀로나", "FRA": "프랑크푸르트",
    "AMS": "암스테르담", "IST": "이스탄불", "ZRH": "취리히", "VIE": "빈", "MUC": "뮌헨",
    "PRG": "프라하", "MAD": "마드리드", "HEL": "헬싱키",
    # 미주
    "JFK": "뉴욕", "LAX": "로스앤젤레스", "HNL": "호놀룰루", "YVR": "밴쿠버", "SEA": "시애틀",
    "ATL": "애틀랜타", "DFW": "댈러스", "IAD": "워싱턴", "LAS": "라스베이거스", "YYZ": "토론토",
    # 오세아니아/태평양
    "GUM": "괌", "SPN": "사이판", "SYD": "시드니",
}


def city_name(code: str) -> str:
    """Korean city name for an airport code; falls back to the code itself
    (logging a warning) when unmapped so a push never breaks on a new route."""
    name = CITY_NAMES.get(code)
    if name is None:
        print(f"WARN: no Korean city name for airport code {code!r}; using code as-is",
              file=sys.stderr)
        return code
    return name


def _date_md(iso: str) -> str:
    """ISO datetime → 'M.DD' (month unpadded, day zero-padded). No year/weekday."""
    d = dt.date.fromisoformat(iso[:10])
    return f"{d.month}.{d.day:02d}"


def _deal_date_token(deal: dict) -> str:
    """Round-trip (return_at present) → '[5.28~6.03]'. One-way → '편도 [6.03]'."""
    dep = _date_md(deal["departure_at"])
    if deal.get("return_at"):
        return f"[{dep}~{_date_md(deal['return_at'])}]"
    return f"편도 [{dep}]"


def format_deal_line(deal: dict) -> str:
    """One bundle line: '인천→마닐라 188,000원 [5.28~6.03]' (왕복) or
    '인천→방콕 245,000원 편도 [6.03]' (편도). Exact price, thousands comma, no %."""
    return (f"{city_name(deal['from'])}→{city_name(deal['destination'])} "
            f"{deal['price']:,}원 {_deal_date_token(deal)}")


def build_bundle_body(deals: list[dict]) -> str:
    """Body for the bundled push: cheapest BUNDLE_TOP_N deals, one per line.
    When more than that match, '외 N건' is appended to the last shown line."""
    ordered = sorted(deals, key=lambda d: d["price"])
    lines = [format_deal_line(d) for d in ordered[:BUNDLE_TOP_N]]
    extra = len(ordered) - len(lines)
    if extra > 0 and lines:
        lines[-1] = f"{lines[-1]} 외 {extra}건"
    return "\n".join(lines)


def bundle_signature(deals: list[dict]) -> str:
    """Identity for bundle dedup. Keyed on the cheapest-N route set + total
    count, NOT on prices/dates — so the 30-min cron's cache wobble doesn't
    re-send an otherwise-identical bundle. A new route entering the top N, or
    the match count changing, produces a new signature (and a fresh push)."""
    ordered = sorted(deals, key=lambda d: d["price"])
    top_keys = sorted(_route_key(d) for d in ordered[:BUNDLE_TOP_N])
    return f"BUNDLE|{len(deals)}|" + ",".join(top_keys)


def _matches_user(
    deal: dict,
    user: dict,
    today: dt.date | None = None,
) -> tuple[bool, str]:
    """Return (should_push, reason). reason kept for logging/debug.

    Filters in order: master off → departure validity → past/future check →
    alarm_window range (when set) → origins → destinations → tier (only "green"
    pushes). Earliest fail wins. (Step 3: the per-user discount cut was removed —
    the deal decision is made once, in snapshot.py's near-floor judge. Dedup
    moved out: it's now per-bundle, applied in main() via bundle_signature.
    Non-green deals are dropped from push here but still ship in deals.json.)

    Departure date checks (existence + not-past) ALWAYS apply — we never push
    a deal we can't anchor in time. alarm_window only controls the upper
    bound: '7' / '30' enforce a max days-until-departure; None means no upper
    bound (UI sends NULL when both 7d/30d toggles are off — user wants all
    valid future deals regardless of how far out). Date compare uses UTC;
    ±1d boundary acceptable per spec.
    """
    if not user.get("alarm_master"):
        return False, "alarm_off"

    # Departure date: required regardless of alarm_window. Cache can carry
    # stale entries (past departures, missing fields) — never push those.
    dep_raw = deal.get("departure_at")
    if not dep_raw:
        return False, "no_departure_date"
    try:
        # Take the date prefix of the ISO string. Spec accepts ±1d boundary
        # error from ignoring the offset; this keeps the comparison simple.
        dep_date = dt.date.fromisoformat(dep_raw[:10])
    except ValueError:
        return False, "no_departure_date"

    if today is None:
        today = dt.datetime.now(dt.timezone.utc).date()
    days_until = (dep_date - today).days
    if days_until < 0:
        return False, "past_departure"

    # alarm_window: upper-bound check only. NULL = no upper bound.
    window = user.get("alarm_window")
    if window is not None:
        try:
            limit = int(window)
        except (TypeError, ValueError):
            # Unknown alarm_window value — block defensively rather than guess.
            return False, "alarm_window_invalid"
        if days_until > limit:
            return False, f"outside_{limit}d_window"

    origins = user.get("origins") or []
    if origins and deal["from"] not in origins:
        return False, "origin_filtered"

    # Empty destinations array = "all destinations" (user hasn't narrowed it).
    destinations = user.get("destinations") or []
    if destinations and deal["destination"] not in destinations:
        return False, "destination_filtered"

    # Step 3: the deal decision lives entirely in snapshot.py (near-floor
    # judge). trigger no longer re-filters on a per-user discount percentage,
    # and dedup is now per-bundle (main() via bundle_signature). Only the
    # "green" tier (below the floor) is worth a push; regular deals (tier None,
    # floor..+20%) still ship in deals.json for the app but don't notify.
    if deal.get("tier") != "green":
        return False, "non_green_no_push"

    return True, "ok"


# ---------- I/O glue ----------

def _load_deals(repo_root: Path) -> list[dict]:
    deals_path = repo_root / "deals.json"
    if not deals_path.exists():
        print("ERROR: deals.json not found — did snapshot.py run first?", file=sys.stderr)
        sys.exit(1)
    with deals_path.open("r", encoding="utf-8") as f:
        return json.load(f).get("deals", [])


def _format_summary(stats: dict, bundle_plan: list[tuple]) -> str:
    """Workflow-log friendly summary. bundle_plan: list of
    (token, title, body, signature, top_deal) — one bundled push per user."""
    lines = [
        "# Trigger evaluation (bundled per-user)",
        f"- active users: {stats['users']}",
        f"- deals in feed: {stats['deals']}",
        f"- pairs evaluated: {stats['pairs']}",
        f"- bundles planned: {len(bundle_plan)}",
        f"- users skipped (no match): {stats['skip_nomatch']}",
        f"- users skipped (dup bundle): {stats['skip_dup']}",
    ]
    for reason, count in sorted(stats["reasons"].items()):
        lines.append(f"  - {reason}: {count}")
    if bundle_plan:
        lines.append("\n## Will push:")
        for token, _title, body, sig, _top in bundle_plan[:30]:  # cap log noise
            tok_short = token.split("[")[-1].rstrip("]")[:18]
            first_line = body.split("\n", 1)[0]
            lines.append(f"- {tok_short}  [{sig}]  {first_line}")
        if len(bundle_plan) > 30:
            lines.append(f"... and {len(bundle_plan) - 30} more")
    return "\n".join(lines)


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    deals = _load_deals(repo_root)

    # Lazy import so unit tests of pure helpers don't need supabase/requests.
    from push import (
        load_active_users,
        load_recent_sent_per_user,
        send_pushes,
        record_sent,
        cleanup_push_history,
    )

    now = dt.datetime.now(dt.timezone.utc)
    today = now.date()
    dry_run = os.getenv("TRIGGER_DRY_RUN") == "1"
    push_failed = False

    try:
        users = load_active_users()
    except Exception as e:
        print(f"ERROR: could not load users ({e})", file=sys.stderr)
        return 1

    if users:
        # Pull per-user sent history in one shot. Set of "token|signature"
        # strings (signature = bundle identity) for O(1) dedup lookup.
        try:
            recent_bundles = load_recent_sent_per_user(days=PUSH_DEDUP_DAYS)
        except Exception as e:
            print(f"WARN: could not load sent history ({e}); proceeding empty.", file=sys.stderr)
            recent_bundles = set()

        # One bundled push per user: collect their matching deals, then emit a
        # single notification (cheapest first) instead of one push per deal.
        bundle_plan: list[tuple] = []
        stats = {
            "users": len(users), "deals": len(deals), "pairs": 0,
            "reasons": {}, "skip_nomatch": 0, "skip_dup": 0,
        }

        for user in users:
            matched: list[dict] = []
            for deal in deals:
                stats["pairs"] += 1
                ok, reason = _matches_user(deal, user, today=today)
                stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
                if ok:
                    matched.append(deal)

            if not matched:
                stats["skip_nomatch"] += 1
                continue

            matched.sort(key=lambda d: d["price"])
            sig = bundle_signature(matched)
            # Per-bundle dedup: same user + same bundle signature in last N days.
            if f"{user['token']}|{sig}" in recent_bundles:
                stats["skip_dup"] += 1
                continue

            bundle_plan.append((
                user["token"], BUNDLE_TITLE, build_bundle_body(matched), sig, matched[0],
            ))

        print(_format_summary(stats, bundle_plan))

        if bundle_plan and dry_run:
            print("\n(DRY RUN — not sending)")
        elif bundle_plan:
            # Isolate the delivery phase: a Supabase/Expo failure here must not
            # crash the workflow with a raw traceback. Surface it as a GitHub
            # ::error:: annotation instead. With deal data already published
            # (this step runs last), a red job is now an alert, not data loss.
            try:
                sent = send_pushes(bundle_plan)
            except Exception as e:
                print(f"::error::Expo send failed: {e!r}", file=sys.stderr)
                sent, push_failed = [], True
            if sent:
                try:
                    record_sent(sent, now)
                except Exception as e:
                    # Pushes already went out but dedup rows weren't persisted →
                    # the same bundle could be re-sent next run. Flag loudly.
                    push_failed = True
                    print(f"::error::record_sent failed AFTER sending {len(sent)} "
                          f"bundle(s) — dedup NOT persisted, duplicate risk next run: {e!r}",
                          file=sys.stderr)
    else:
        print("# Trigger evaluation\n- no active users; skipping push.")

    # Trim old push_history rows. dedup needs only PUSH_DEDUP_DAYS, but we keep
    # PUSH_HISTORY_RETENTION_DAYS for audit/debug. Errors stay isolated so a
    # cleanup hiccup never affects the push cycle that just ran.
    try:
        deleted = cleanup_push_history(PUSH_HISTORY_RETENTION_DAYS, dry_run=dry_run)
        verb = "would delete" if dry_run else "deleted"
        print(f"push_history cleanup: {verb} {deleted} rows older than {PUSH_HISTORY_RETENTION_DAYS}d")
    except Exception as e:
        print(f"WARN: cleanup_push_history failed: {e}", file=sys.stderr)

    # Non-zero so a delivery/record failure shows up as a red run (alert). Safe
    # now that publishing happens before this step — a red job no longer means
    # lost deal data, just that notifications need attention.
    return 1 if push_failed else 0


# ---------- Exposed for tests ----------

# Pure functions are the testable interface; importable without supabase/requests.
__all__ = [
    "_matches_user", "_route_key", "main",
    "city_name", "format_deal_line", "build_bundle_body", "bundle_signature",
    "BUNDLE_TITLE", "BUNDLE_TOP_N",
]


if __name__ == "__main__":
    raise SystemExit(main())
