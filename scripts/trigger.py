"""
Push notification trigger logic.

Pure function design (per project policy):
- evaluate_deals() takes data in, returns decisions + diagnostics out.
- No I/O, no env reads, no clock calls — fully testable.
- All thresholds come from config.py (single source of truth).
- Returns diagnostics for every deal (kept/dropped + reason) so
  thresholds can be backtested and tuned later without code changes.

The CLI entry point at the bottom wires up the I/O (read deals.json,
read recent-sent history from Supabase, write push, log to Supabase).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Iterable

# Local import — config.py lives next to this file.
from config import (
    PUSH_RARE_DEAL_THRESHOLD_PCT,
    PUSH_MIN_SAMPLES,
    PUSH_DEDUP_DAYS,
)


# ---------- Data model ----------

@dataclasses.dataclass
class TriggerDecision:
    """One verdict per input deal. Always carries diagnostics — even when
    dropped — so we can later answer 'why didn't this fire?' from logs."""
    should_notify: bool
    reason: str                  # short tag: "ok", "below_cut", "low_n", "dup_recent"
    deal: dict                   # original deal dict, untouched
    diagnostics: dict            # numbers used in the decision


# ---------- Pure logic ----------

def _route_key(deal: dict) -> tuple[str, str, str]:
    """Identity for dedup. (from, destination, trip) — round-trip ICN-BKK
    and one-way ICN-BKK are different products, so trip is part of the key."""
    return (deal["from"], deal["destination"], deal["trip"])


def _is_recently_sent(deal: dict, sent_history: dict, now: dt.datetime, dedup_days: int) -> bool:
    """sent_history: {route_key_str: iso_timestamp_string} — last push per route.
    A route is suppressed if we pushed it within dedup_days. Idempotency guard
    against repeated runs and noisy near-identical re-quotes."""
    key = "|".join(_route_key(deal))
    last = sent_history.get(key)
    if not last:
        return False
    try:
        last_dt = dt.datetime.fromisoformat(last.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (now - last_dt).total_seconds() < dedup_days * 86400


def evaluate_deals(
    deals: Iterable[dict],
    sent_history: dict,
    now: dt.datetime,
    *,
    threshold_pct: float = PUSH_RARE_DEAL_THRESHOLD_PCT,
    min_samples: int = PUSH_MIN_SAMPLES,
    dedup_days: int = PUSH_DEDUP_DAYS,
) -> list[TriggerDecision]:
    """Decide which deals should fire a push notification.

    deals: list of dicts straight out of deals.json (already passed the
        25/15% display cut in snapshot.py — we apply the stricter push cut here).
    sent_history: {"FROM|DEST|trip": iso_ts} of last successful push per route.
    now: current UTC time. Injected (not called inside) for testability.

    Thresholds are kwargs so tests can sweep them without touching config.
    Defaults pull from config — production callers don't pass these.
    """
    decisions: list[TriggerDecision] = []
    for deal in deals:
        discount = float(deal.get("discount_pct", 0))
        # 'n' (sample size that produced the baseline) isn't in deals.json today.
        # Snapshot writes it to SQLite but not to the public JSON. Until the
        # snapshot script is extended to include it, we treat absence as
        # 'unknown' and let it pass the n-gate. When `n` IS present, enforce it.
        n = deal.get("n")

        diag = {
            "discount_pct": discount,
            "threshold_pct": threshold_pct,
            "n": n,
            "min_samples": min_samples,
            "route": "-".join(_route_key(deal)),
        }

        if discount < threshold_pct:
            decisions.append(TriggerDecision(False, "below_cut", deal, diag))
            continue

        if n is not None and n < min_samples:
            decisions.append(TriggerDecision(False, "low_n", deal, diag))
            continue

        if _is_recently_sent(deal, sent_history, now, dedup_days):
            decisions.append(TriggerDecision(False, "dup_recent", deal, diag))
            continue

        decisions.append(TriggerDecision(True, "ok", deal, diag))

    return decisions


def format_notification(deal: dict) -> tuple[str, str]:
    """Build the push title + body. Kept pure so tests can pin copy."""
    pct = round(float(deal["discount_pct"]))
    price_man = round(deal["price"] / 10000)  # 만원 단위, 한국 사용자 직관적
    trip_tag = "왕복" if deal["trip"] == "roundtrip" else "편도"
    title = f"✈️ {deal['from']} → {deal['destination']} {pct}% 할인"
    body = f"{trip_tag} {price_man}만원 · {deal.get('airline', '')}"
    return title, body


# ---------- CLI / I/O wiring (not part of the pure core) ----------

def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _format_summary(decisions: list[TriggerDecision]) -> str:
    """Human-readable summary for the workflow log."""
    by_reason: dict[str, int] = {}
    for d in decisions:
        by_reason[d.reason] = by_reason.get(d.reason, 0) + 1
    fire = [d for d in decisions if d.should_notify]
    lines = [f"# Trigger evaluation", f"- evaluated: {len(decisions)}"]
    for reason, count in sorted(by_reason.items()):
        lines.append(f"- {reason}: {count}")
    if fire:
        lines.append("\n## Will push:")
        for d in fire:
            lines.append(
                f"- {d.diagnostics['route']}  "
                f"{d.diagnostics['discount_pct']:.1f}%  "
                f"{d.deal['price']:,}원"
            )
    return "\n".join(lines)


def main() -> int:
    """Wire: read deals.json + sent history → evaluate → push → log.

    Designed so failure to read history (e.g. Supabase down) doesn't block
    pushes — we fall back to empty history. The dedup table being temporarily
    empty just means we might re-send something; far better than silence.
    """
    repo_root = Path(__file__).resolve().parent.parent
    deals_path = repo_root / "deals.json"
    if not deals_path.exists():
        print("ERROR: deals.json not found — did snapshot.py run first?", file=sys.stderr)
        return 1

    data = _load_json(deals_path)
    deals = data.get("deals", [])

    # Lazy imports so the unit-test path doesn't need supabase/requests installed.
    from push import load_recent_sent, send_pushes, record_sent

    now = dt.datetime.now(dt.timezone.utc)

    try:
        sent_history = load_recent_sent(days=PUSH_DEDUP_DAYS)
    except Exception as e:
        print(f"WARN: could not load sent history ({e}); proceeding with empty.", file=sys.stderr)
        sent_history = {}

    decisions = evaluate_deals(deals, sent_history, now)
    print(_format_summary(decisions))

    to_push = [d for d in decisions if d.should_notify]
    if not to_push:
        return 0

    if os.getenv("TRIGGER_DRY_RUN") == "1":
        print("\n(DRY RUN — not sending)")
        return 0

    sent = send_pushes(to_push)
    record_sent(sent, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
