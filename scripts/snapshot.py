import datetime as dt
import json
import os
import re
import statistics
import time
import urllib.parse
from pathlib import Path

import dealdb
import realtime
import tpclient
from config import (
    BASELINE_WINDOW_DAYS,
    BLOCKED_GATES,
    DESTINATIONS,
    MAX_CACHE_AGE_DAYS,
    MAX_ERROR_RATE,
    MAX_PRICE_DIVERGENCE_PCT,
    MIN_HISTORY_DAYS,
    MIN_HOURS_BEFORE_DEPARTURE,
    ORIGINS,
    OUTLIER_DROP_TOP_PCT,
    OUTLIER_MIN_N,
    REALTIME_CROSSCHECK,
    REALTIME_REQUEST_DELAY_SEC,
    ROUNDTRIP_VS_ONEWAY_MEDIAN_RATIO,
    SANITY_MAX_DISCOUNT_PCT,
    TIER_ORANGE_PCT,
    TIER_RED_PCT,
    TRIPS,
)

DEALS_JSON = Path(__file__).resolve().parent.parent / "deals.json"
REQUEST_DELAY_SEC = 0.5


def scrub_secret(text, secret):
    return text.replace(secret, "***") if secret else text


# One-off diagnostic: dump raw Travelpayouts items for a few target routes so
# we can see field names (esp. cabin-related), value distributions, and price
# spread. Triggered by DUMP_RAW_API=1 (set via workflow_dispatch diagnose_api
# input). Removed in Step 2 once the cabin question is answered.
DUMP_RAW_API = os.environ.get("DUMP_RAW_API") == "1"
DUMP_ROUTES = {("GMP", "CEB"), ("GMP", "KLO"), ("GMP", "FUK")}


def _diag_dump_raw(origin, dest, trip_label, items, stage):
    """Dump field names, cabin distribution, price stats, and top/bottom 5
    fares (whitelisted fields only — 'link' deliberately excluded so deep-link
    fare tokens don't leak into workflow logs). Items contain no API token;
    the token only lives in the request header (tpclient._get_data)."""
    print(f"\n# === RAW DUMP {origin}->{dest} {trip_label} ({stage}) ===")
    print(f"# items count: {len(items)}")
    if not items:
        print("# (empty)\n# === END DUMP ===\n")
        return
    print(f"# fields in first item: {sorted(items[0].keys())}")
    cabin_like = [k for k in items[0].keys() if "class" in k.lower() or "cabin" in k.lower()]
    print(f"# cabin-like fields: {cabin_like}")
    for fld in cabin_like:
        dist = {}
        for it in items:
            dist[it.get(fld)] = dist.get(it.get(fld), 0) + 1
        print(f"# distribution of '{fld}': {dist}")
    valid = [it for it in items if isinstance(it.get("price"), (int, float))]
    if valid:
        valid.sort(key=lambda x: x["price"])
        n = len(valid)
        print(f"# price stats: n={n} min={valid[0]['price']:,} "
              f"median={valid[n//2]['price']:,} max={valid[-1]['price']:,}")
        SAFE_KEYS = ["price", "airline", "transfers", "return_transfers",
                     "duration", "duration_to", "duration_back",
                     "departure_at", "return_at"] + cabin_like
        print("# bottom 5 cheapest:")
        for it in valid[:5]:
            print(f"#   {json.dumps({k: it.get(k) for k in SAFE_KEYS if k in it}, ensure_ascii=False)}")
        print("# top 5 expensive:")
        for it in valid[-5:][::-1]:
            print(f"#   {json.dumps({k: it.get(k) for k in SAFE_KEYS if k in it}, ensure_ascii=False)}")
    print("# === END DUMP ===\n")


def summarize(items):
    prices = sorted(
        it["price"] for it in items if isinstance(it.get("price"), (int, float))
    )
    if not prices:
        return None
    return {
        "n": len(prices),
        "min": prices[0],
        "p25": statistics.quantiles(prices, n=4)[0] if len(prices) >= 4 else prices[0],
        "median": statistics.median(prices),
        "mean": round(statistics.mean(prices)),
    }


def cheapest_item(items):
    valid = [it for it in items if isinstance(it.get("price"), (int, float))]
    return min(valid, key=lambda it: it["price"]) if valid else None


def freshness_index(latest_items):
    """Map (gate, price, departure_at) -> latest-prices entry. get_latest_prices
    mirrors prices_for_dates fare-for-fare but carries the freshness fields
    (actual, found_at) that prices_for_dates omits; the price/depart fields are
    named value/depart_date there."""
    return {
        (it.get("gate"), it.get("value"), it.get("depart_date")): it
        for it in latest_items
    }


def is_stale(latest, now):
    """A matched latest-prices entry is stale if the seller marks it not current
    (actual is False) or it was last found at least MAX_CACHE_AGE_DAYS ago."""
    if latest.get("actual") is False:
        return True
    found_at = latest.get("found_at")
    if found_at:
        try:
            found_dt = dt.datetime.fromisoformat(found_at.replace("Z", "+00:00"))
            if (now - found_dt).total_seconds() / 86400 >= MAX_CACHE_AGE_DAYS:
                return True
        except (ValueError, TypeError):
            pass
    return False


def filter_items(items, latest_items, now):
    """Drop fares we don't trust enough to alert on: those from a low-trust gate,
    those departing too soon to realistically book, and those the latest-prices
    endpoint marks stale/expired (cross-checked, since prices_for_dates carries
    the booking link but no freshness fields). Unmatched fares are kept so a
    missing/failed freshness lookup degrades gracefully instead of emptying the
    feed. Applied before summarizing so neither the deal price nor the baseline
    is built from these fares."""
    fresh = freshness_index(latest_items)
    kept = []
    for it in items:
        if it.get("gate") in BLOCKED_GATES:
            continue
        depart_at = it.get("departure_at")
        if depart_at:
            try:
                hours_left = (dt.datetime.fromisoformat(depart_at) - now).total_seconds() / 3600
                if hours_left < MIN_HOURS_BEFORE_DEPARTURE:
                    continue
            except (ValueError, TypeError):
                pass
        latest = fresh.get((it.get("gate"), it.get("price"), it.get("departure_at")))
        if latest is not None and is_stale(latest, now):
            continue
        kept.append(it)
    return kept


def filter_price_outliers(items, drop_top_pct=OUTLIER_DROP_TOP_PCT):
    """Cabin-mix protector (Step 2-A-0). Returns (kept_items, dropped_prices).

    Heuristic: sort items by price ascending, drop the top drop_top_pct
    fraction. Works even when contaminating fares are the majority — a
    median-based filter can't, because the median is itself contaminated in
    that case. Items with non-numeric price are passed through untouched.

    No-op when items has 0 or 1 valid prices (nothing to compare against).
    Guard2 in judge() catches over-aggressive trimming via the post-filter
    items_count >= 5 requirement.
    """
    valid = [it for it in items if isinstance(it.get("price"), (int, float))]
    if len(valid) <= 1:
        return list(items), []
    valid.sort(key=lambda x: x["price"])
    keep_count = max(1, int(len(valid) * (1 - drop_top_pct)))
    kept_valid = valid[:keep_count]
    dropped_prices = [x["price"] for x in valid[keep_count:]]
    # Preserve non-numeric-price items defensively (filter_items shouldn't
    # leave any, but if it does they're untouched here).
    nonnumeric = [it for it in items if not isinstance(it.get("price"), (int, float))]
    return kept_valid + nonnumeric, dropped_prices


def _deal_tier(min_price, baseline):
    """Color tier by price vs the floor, or None if not a deal.
      green  : below the floor
      orange : floor .. floor +TIER_ORANGE_PCT
      red    : floor +TIER_ORANGE_PCT .. floor +TIER_RED_PCT
    """
    if min_price < baseline:
        return "green"
    if min_price <= baseline * (1 + TIER_ORANGE_PCT / 100):
        return "orange"
    if min_price <= baseline * (1 + TIER_RED_PCT / 100):
        return "red"
    return None


def judge(stats, history, today_items_count):
    """Compute (baseline, discount, is_deal, diag) under the near-floor tiers.

    Baseline = mean of the 5 lowest daily minimums within the rolling window
    (caller windows the history via dealdb.historical_mins).

    Deal decision (Step 3): is_deal iff current min <= baseline ×
    (1 + TIER_RED_PCT/100). Each deal gets a color tier (diag["tier"]):
    green (below floor) / orange (floor..+TIER_ORANGE_PCT) /
    red (+TIER_ORANGE_PCT..+TIER_RED_PCT). `discount`
    ((baseline-min)/baseline×100) is kept for display/logging and can be
    negative (orange/red sit above the floor).

    Guards (each forces is_deal=False; diag.guard_triggered names the one):
      - "history": fewer than MIN_HISTORY_DAYS daily mins in window → warmup
      - "today_n": fewer than 5 fares after filter_items + outlier filter
      - "sanity": discount > SANITY_MAX_DISCOUNT_PCT (min far below floor) →
        almost always residual contamination; WARN log

    today_items_count is the count AFTER filter_price_outliers — caller
    passes stats["n"] (summarize() runs on the already-filtered list).
    """
    diag = {
        "baseline_value": None,
        "baseline_method": "rolling_n5_lowest",
        "history_days_used": len(history),
        "today_items_count_after_outlier_filter": today_items_count,
        "guard_triggered": None,
        "tier": None,               # green / orange / red (set below when a deal)
        "cabin_class": "economy",   # Step 2-A-5: always economy (trip_class=0
                                    # + price outlier guard; API doesn't expose
                                    # cabin, so we label the survivors).
    }

    if len(history) < MIN_HISTORY_DAYS:
        diag["guard_triggered"] = "history"
        return None, 0.0, False, diag

    if today_items_count < 5:
        diag["guard_triggered"] = "today_n"
        return None, 0.0, False, diag

    baseline = statistics.mean(sorted(history)[:5])
    diag["baseline_value"] = baseline
    discount = (baseline - stats["min"]) / baseline * 100

    if discount > SANITY_MAX_DISCOUNT_PCT:
        print(f"WARN: sanity guard fired — baseline={baseline:,.0f} "
              f"min={stats['min']:,} discount={discount:.1f}%")
        diag["guard_triggered"] = "sanity"
        return baseline, discount, False, diag

    tier = _deal_tier(stats["min"], baseline)
    diag["tier"] = tier
    return baseline, discount, tier is not None, diag


def main():
    token = tpclient.get_token()
    today = dt.date.today().isoformat()
    now = dt.datetime.now(dt.timezone.utc)
    conn = dealdb.connect()

    results = []
    for origin in ORIGINS:
        for dest in DESTINATIONS:
            for trip_label, one_way in TRIPS:
                try:
                    items = tpclient.fetch_prices(origin, dest, one_way, token)
                except tpclient.TPAuthError:
                    raise   # dead token → abort whole run before publishing
                except Exception as e:
                    msg = scrub_secret(repr(e), token)
                    results.append({"origin": origin, "dest": dest, "trip": trip_label, "status": f"error: {msg}"})
                    continue
                finally:
                    time.sleep(REQUEST_DELAY_SEC)

                # Freshness oracle: get_latest_prices carries actual/found_at. If
                # it fails, fall back to no freshness filtering this round
                # (degrade gracefully rather than drop the whole route).
                try:
                    latest = tpclient.fetch_latest(origin, dest, one_way, token)
                except tpclient.TPAuthError:
                    raise   # dead token → abort whole run before publishing
                except Exception:
                    latest = []
                finally:
                    time.sleep(REQUEST_DELAY_SEC)

                if DUMP_RAW_API and (origin, dest) in DUMP_ROUTES and trip_label == "roundtrip":
                    _diag_dump_raw(origin, dest, trip_label, items, "pre-filter")
                items = filter_items(items, latest, now)
                if DUMP_RAW_API and (origin, dest) in DUMP_ROUTES and trip_label == "roundtrip":
                    _diag_dump_raw(origin, dest, trip_label, items, "post-filter")

                # Cabin-mix protector: drop the top OUTLIER_DROP_TOP_PCT by
                # price before judging. The pre-filter items would still
                # include any business/first fares the cache returns despite
                # trip_class=0 (Travelpayouts cache partially ignores it).
                items, dropped_outlier_prices = filter_price_outliers(items)
                if dropped_outlier_prices:
                    sample = [f"{p:,}" for p in dropped_outlier_prices[:5]]
                    extra = f" (+{len(dropped_outlier_prices)-5} more)" if len(dropped_outlier_prices) > 5 else ""
                    print(f"WARN: {origin}->{dest} {trip_label} outlier filter "
                          f"removed {len(dropped_outlier_prices)} fares: {sample}{extra}")

                stats = summarize(items)
                if not stats:
                    results.append({"origin": origin, "dest": dest, "trip": trip_label, "status": "no-data"})
                    continue

                cheap = cheapest_item(items)
                dealdb.upsert_snapshot(conn, {
                    "snapshot_date": today,
                    "origin": origin,
                    "destination": dest,
                    "trip": trip_label,
                    "n": stats["n"],
                    "min_price": stats["min"],
                    "p25": stats["p25"],
                    "median": stats["median"],
                    "mean": stats["mean"],
                    "cheapest_depart_at": cheap.get("departure_at"),
                    "cheapest_return_at": cheap.get("return_at") or None,
                    "cheapest_airline": cheap.get("airline"),
                    "cheapest_gate": cheap.get("gate"),
                    "cheapest_link": cheap.get("link"),
                })

                # Rolling-window baseline: only daily-mins within the last
                # BASELINE_WINDOW_DAYS feed the floor (older rows stay in DB).
                history = dealdb.historical_mins(
                    conn, origin, dest, trip_label, today,
                    window_days=BASELINE_WINDOW_DAYS,
                )
                baseline, discount, is_deal, diag = judge(stats, history, stats["n"])
                # Caller-side diag enrichment: outlier counts known here.
                diag["outliers_removed_count"] = len(dropped_outlier_prices)
                diag["outliers_removed_prices"] = list(dropped_outlier_prices)
                results.append({
                    "origin": origin, "dest": dest, "trip": trip_label, "status": "ok",
                    "min": stats["min"], "median": stats["median"], "n": stats["n"],
                    "baseline": baseline, "discount": discount, "tier": diag.get("tier"),
                    "is_deal": is_deal, "diag": diag, "cheap": cheap,
                })

    conn.close()
    drop_impossible_roundtrips(results)
    crosscheck_realtime(results)
    report(results, today)            # diagnostics always reach the log
    _guard_publish_safety(results)    # abort here (pre-write) if the run looks broken
    write_deals_json(results)


def crosscheck_realtime(results):
    """Drop deal candidates whose live Google Flights price (via fast-flights) is
    at least MAX_PRICE_DIVERGENCE_PCT above the cached Travelpayouts price -- a
    large gap means the cached fare is likely stale/unbookable. Every failure
    path (FX lookup, scrape error/timeout, missing dependency, no price) keeps
    the candidate, so a flaky check never empties the feed. Drops are logged."""
    if not REALTIME_CROSSCHECK:
        print("\n# realtime cross-check: disabled (REALTIME_CROSSCHECK=False)")
        return
    deals = select_deals(results)
    print(f"\n# realtime cross-check: {len(deals)} candidate(s)")
    if not deals:
        return
    try:
        import fast_flights  # noqa: F401
    except Exception as e:
        print(f"#   SKIPPED: fast-flights unavailable, all candidates kept ({e!r})")
        return
    try:
        fx = realtime.usd_to_krw()
    except Exception as e:
        print(f"#   SKIPPED: FX lookup failed, all candidates kept ({e!r})")
        return
    print(f"#   FX {fx:,.1f} KRW/USD")
    for r in deals:
        cheap = r["cheap"]
        depart = (cheap.get("departure_at") or "")[:10]
        ret = (cheap.get("return_at") or "")[:10] if r["trip"] == "roundtrip" else None
        if not depart:
            print(f"#   keep {r['origin']}->{r['dest']} [{r['trip']}] (no departure date)")
            continue
        try:
            live = realtime.cheapest_krw(r["origin"], r["dest"], depart, ret, fx)
        except Exception as e:
            print(f"#   keep {r['origin']}->{r['dest']} [{r['trip']}] (live lookup failed: {e!r})")
            continue
        finally:
            time.sleep(REALTIME_REQUEST_DELAY_SEC)
        if not live:
            print(f"#   keep {r['origin']}->{r['dest']} [{r['trip']}] (no live price found)")
            continue
        divergence = (live - r["min"]) / r["min"] * 100
        r["realtime_krw"] = live
        r["divergence_pct"] = round(divergence, 1)
        if divergence >= MAX_PRICE_DIVERGENCE_PCT:
            r["is_deal"] = False
            r["realtime_note"] = f"REALTIME-GAP TP {r['min']:,} vs live {live:,} (+{divergence:.0f}%)"
            print(f"#   DROP {r['origin']}->{r['dest']} [{r['trip']}] TP {r['min']:,} vs live {live:,} (+{divergence:.0f}%)")
        else:
            print(f"#   keep {r['origin']}->{r['dest']} [{r['trip']}] TP {r['min']:,} vs live {live:,} ({divergence:+.0f}%)")


def select_deals(results):
    return [r for r in results if r.get("status") == "ok" and r["is_deal"]]


def _guard_publish_safety(results):
    """Abort (non-zero exit) before publishing when the run looks broken — dead
    token, mass API outage, or zero deals — so the last good feed isn't
    overwritten with an empty/garbage one. The workflow's commit+mirror steps are
    skipped when the snapshot step fails. Only data-API fetch failures count
    toward the error rate; realtime-crosscheck failures keep the candidate and
    never set an error status, so they don't trip this."""
    total = len(results)
    errors = sum(1 for r in results if str(r.get("status", "")).startswith("error"))
    if total and errors / total > MAX_ERROR_RATE:
        print(f"::error::{errors}/{total} routes failed (> {MAX_ERROR_RATE:.0%}); "
              f"aborting before publish to keep the previous feed")
        raise SystemExit(3)
    if not select_deals(results):
        print("::error::0 deals after judging; aborting before publish to keep the "
              "previous feed (check token/cache/guards)")
        raise SystemExit(4)


def cache_date_from_link(link):
    """Travelpayouts deeplinks embed the cache search date as search_date=DDMMYYYY;
    expose it as an ISO date so the site can show/flag how fresh a fare is."""
    m = re.search(r"search_date=(\d{2})(\d{2})(\d{4})", link or "")
    if not m:
        return None
    dd, mm, yyyy = m.groups()
    return f"{yyyy}-{mm}-{dd}"


def strip_link_params(link):
    """Drop tracking/comparison params from the deeplink: expected_price* (which
    make Aviasales compare against our cached number and can surface a 'price
    changed' state) and static_fare_key. The fare token (t=) and search params
    remain so the link still resolves to the fare and just shows the live price."""
    if not link or "?" not in link:
        return link
    path, query = link.split("?", 1)
    kept = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True)
        if not (k.startswith("expected_price") or k == "static_fare_key")
    ]
    return f"{path}?{urllib.parse.urlencode(kept)}"


def write_deals_json(results):
    deals = select_deals(results)
    deals.sort(key=lambda r: r["discount"], reverse=True)
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "origin": ORIGINS[0],
        "origins": ORIGINS,
        "currency": "KRW",
        # Tier cutoffs (% above the floor) that map to each deal's color label.
        # green = below floor; orange = 0..orange; red = orange..red.
        "tier_thresholds_pct": {"orange": TIER_ORANGE_PCT, "red": TIER_RED_PCT},
        "refresh_interval_hours": 1,
        "disclaimer": (
            "캐시 기반 가격으로 실시간이 아닙니다. 좌석이 빠르게 팔릴 수 있어 "
            "클릭 시 이미 매진되었거나 가격이 변동됐을 수 있습니다."
        ),
        "deals": [
            {
                "from": r["origin"],
                "destination": r["dest"],
                "trip": r["trip"],
                "price": r["min"],
                "baseline": round(r["baseline"]),
                "discount_pct": round(r["discount"], 1),
                "tier": r["tier"],
                "departure_at": r["cheap"].get("departure_at"),
                "return_at": r["cheap"].get("return_at") or None,
                "transfers": max(r["cheap"].get("transfers") or 0, r["cheap"].get("return_transfers") or 0),
                "airline": r["cheap"].get("airline"),
                "gate": r["cheap"].get("gate"),
                "cache_date": cache_date_from_link(r["cheap"].get("link")),
                "link": strip_link_params(r["cheap"].get("link")),
                "cabin_class": (r.get("diag") or {}).get("cabin_class") or "economy",
            }
            for r in deals
        ],
    }
    DEALS_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def drop_impossible_roundtrips(results):
    """A roundtrip can't realistically cost less than a single one-way leg.
    Flag (and exclude from alerts) any roundtrip priced below its route's
    cheapest one-way, or below ROUNDTRIP_VS_ONEWAY_MEDIAN_RATIO of the one-way
    median -- both are clear cache errors. One-way fares are never touched, so
    genuine deep deals are preserved; the raw snapshot is still recorded."""
    oneway = {
        (r["origin"], r["dest"]): r
        for r in results
        if r["status"] == "ok" and r["trip"] == "oneway"
    }
    for r in results:
        if r.get("status") != "ok" or r["trip"] != "roundtrip":
            continue
        ow = oneway.get((r["origin"], r["dest"]))
        if not ow:
            continue
        if r["min"] < ow["min"]:
            r["is_deal"] = False
            r["sanity_note"] = f"DATA-ERR rt {r['min']:,} < ow min {ow['min']:,}"
        elif ow["n"] >= OUTLIER_MIN_N and r["min"] < ow["median"] * ROUNDTRIP_VS_ONEWAY_MEDIAN_RATIO:
            r["is_deal"] = False
            r["sanity_note"] = (
                f"DATA-ERR rt {r['min']:,} < {ROUNDTRIP_VS_ONEWAY_MEDIAN_RATIO:.0%} "
                f"of ow median {round(ow['median']):,}"
            )


def report(results, today):
    print(f"# Snapshot {today}  (tiers: green<floor, orange<=+{TIER_ORANGE_PCT:.0f}%, "
          f"red<=+{TIER_RED_PCT:.0f}%)\n")
    header = f"{'Route':<10}{'Trip':<11}{'Min':>10}{'Baseline':>11}{'Disc':>8}  {'Tier':<7} Basis"
    print(header)
    print("-" * len(header))

    for r in results:
        route = f"{r['origin']}->{r['dest']}"
        if r["status"] != "ok":
            print(f"{route:<10}{r['trip']:<11}{'-':>10}{'-':>11}{'-':>8}  {'-':<7} {r['status']}")
            continue
        diag = r.get("diag") or {}
        guard = diag.get("guard_triggered")
        if r.get("sanity_note"):
            basis = r["sanity_note"]
        elif guard:
            basis = (f"GUARD:{guard} (hist={diag.get('history_days_used')}d "
                     f"n_post_outlier={diag.get('today_items_count_after_outlier_filter')})")
        else:
            basis = (f"n5-min hist={diag.get('history_days_used')}d "
                     f"n={diag.get('today_items_count_after_outlier_filter')} "
                     f"floor+{TIER_RED_PCT:.0f}%")
        if r.get("sanity_note"):
            mark = "DROP"
        elif r["is_deal"]:
            mark = (diag.get("tier") or "yes")
        else:
            mark = "no"
        baseline_str = f"{round(r['baseline']):,}" if r['baseline'] is not None else "-"
        print(
            f"{route:<10}{r['trip']:<11}{r['min']:>10,}{baseline_str:>11}"
            f"{r['discount']:>7.1f}%  {mark:<7} {basis}"
        )

    deals = select_deals(results)
    print()
    if not deals:
        print("## 특가 없음")
        return
    print(f"## 특가 {len(deals)}건")
    for r in deals:
        c = r["cheap"]
        ret = f" ~ {c['return_at'][:10]}" if c.get("return_at") else ""
        print(
            f"- [{r['tier']}] {r['origin']}->{r['dest']} [{r['trip']}] {r['min']:,} KRW "
            f"(baseline {round(r['baseline']):,}, vs floor {r['discount']:+.1f}%) "
            f"출발 {(c.get('departure_at') or '')[:10]}{ret} "
            f"{c.get('airline', '')}/{c.get('gate', '')}"
        )


if __name__ == "__main__":
    try:
        main()
    except tpclient.TPAuthError as e:
        print(f"::error::TRAVELPAYOUTS auth failed (HTTP {e.code}) — token rejected; "
              f"aborting before publish, previous feed kept")
        raise SystemExit(2)
