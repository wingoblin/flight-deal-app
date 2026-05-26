"""
Per-user push notification trigger.

Design:
- Pull all active subscribers from Supabase.
- For each (user, deal) pair, evaluate the user's own thresholds and filters.
- Per-user 3-day dedup keyed on (token, route).
- Fire pushes individually so each user gets only deals matching their settings.

Why per-user instead of a single global cut:
- App lets each user pick their own discount cut (short-haul 25%+, long-haul 15%+
  by default, adjustable per-user). A global cut would ignore that choice.

Idempotency / safety:
- Sent-history is checked per (token, route_key) so retries don't re-spam.
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
    DEAL_THRESHOLD_PCT_BY_DEST,
)


# ---------- Pure helpers ----------

def _route_key(deal: dict) -> str:
    """Identity for dedup. Round-trip and one-way to the same city are
    different products, so trip is part of the key."""
    return f"{deal['from']}|{deal['destination']}|{deal['trip']}"


def _is_long_haul(destination: str) -> bool:
    """Long-haul iff destination is in the 15%-threshold list (Europe/Americas).
    Matches the same definition snapshot.py uses for the display cut, so the
    two stages stay consistent."""
    return destination in DEAL_THRESHOLD_PCT_BY_DEST


def _matches_user(
    deal: dict,
    user: dict,
    recent_for_user: set[str],
    today: dt.date | None = None,
) -> tuple[bool, str]:
    """Return (should_push, reason). reason kept for logging/debug.

    Filters in order: master off → alarm_window (incl. departure date) →
    origins → destinations → discount → dedup. Earliest fail wins.

    alarm_window is the user's "days-until-departure" preference: '7' / '30'
    / None. None = both toggles off → block. Compared against the deal's
    departure_at date in UTC (±1d boundary acceptable, per spec).
    """
    if not user.get("alarm_master"):
        return False, "alarm_off"

    window = user.get("alarm_window")
    if window is None:
        return False, "alarm_window_null"

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

    # Per-user discount cut. Long-haul uses disc_long_pct; everything else
    # uses disc_short_pct. Sensible defaults match the app's defaults.
    is_long = _is_long_haul(deal["destination"])
    user_cut = (user.get("disc_long_pct") if is_long else user.get("disc_short_pct")) or 0
    if float(deal.get("discount_pct", 0)) < float(user_cut):
        return False, "below_user_cut"

    # Per-user dedup: same token + route in last N days → skip.
    if f"{user['token']}|{_route_key(deal)}" in recent_for_user:
        return False, "dup_recent"

    return True, "ok"


# ---------- I/O glue ----------

def _load_deals(repo_root: Path) -> list[dict]:
    deals_path = repo_root / "deals.json"
    if not deals_path.exists():
        print("ERROR: deals.json not found — did snapshot.py run first?", file=sys.stderr)
        sys.exit(1)
    with deals_path.open("r", encoding="utf-8") as f:
        return json.load(f).get("deals", [])


def _format_summary(stats: dict, push_plan: list[tuple]) -> str:
    """Workflow-log friendly summary. push_plan: list of (token, deal, reason, lang)."""
    lines = [
        "# Trigger evaluation (per-user)",
        f"- active users: {stats['users']}",
        f"- deals in feed: {stats['deals']}",
        f"- pairs evaluated: {stats['pairs']}",
        f"- pushes planned: {len(push_plan)}",
    ]
    for reason, count in sorted(stats["reasons"].items()):
        lines.append(f"  - {reason}: {count}")
    if push_plan:
        lines.append("\n## Will push:")
        for token, deal, _, _ in push_plan[:30]:  # cap log noise
            tok_short = token.split("[")[-1].rstrip("]")[:18]
            lines.append(
                f"- {tok_short}  {deal['from']}→{deal['destination']}  "
                f"{deal['discount_pct']:.1f}%  {deal['price']:,}원"
            )
        if len(push_plan) > 30:
            lines.append(f"... and {len(push_plan) - 30} more")
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

    try:
        users = load_active_users()
    except Exception as e:
        print(f"ERROR: could not load users ({e})", file=sys.stderr)
        return 1

    if users:
        # Pull per-user sent history in one shot. Set of "token|route_key"
        # strings to make membership check O(1) per pair.
        try:
            recent_for_user = load_recent_sent_per_user(days=PUSH_DEDUP_DAYS)
        except Exception as e:
            print(f"WARN: could not load sent history ({e}); proceeding empty.", file=sys.stderr)
            recent_for_user = set()

        push_plan: list[tuple] = []
        stats = {"users": len(users), "deals": len(deals), "pairs": 0, "reasons": {}}

        for user in users:
            for deal in deals:
                stats["pairs"] += 1
                ok, reason = _matches_user(deal, user, recent_for_user, today=today)
                stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
                if ok:
                    # Pass user's lang through to send_pushes so _format_notification
                    # picks the right wording without a second user lookup.
                    push_plan.append((user["token"], deal, reason, user.get("lang") or "ko"))

        print(_format_summary(stats, push_plan))

        if push_plan and dry_run:
            print("\n(DRY RUN — not sending)")
        elif push_plan:
            sent = send_pushes(push_plan)
            record_sent(sent, now)
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

    return 0


# ---------- Exposed for tests ----------

# Pure functions are the testable interface; importable as
#   from trigger import _matches_user, _route_key, _is_long_haul
__all__ = ["_matches_user", "_route_key", "_is_long_haul", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
