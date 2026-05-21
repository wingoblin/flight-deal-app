import datetime as dt
import json
import re
import statistics
import time
import urllib.parse
from pathlib import Path

import dealdb
import realtime
import tpclient
from config import (
    BLOCKED_GATES,
    DEAL_THRESHOLD_PCT,
    deal_threshold,
    DESTINATIONS,
    MAX_CACHE_AGE_DAYS,
    MAX_PRICE_DIVERGENCE_PCT,
    MIN_HISTORY_DAYS,
    MIN_HOURS_BEFORE_DEPARTURE,
    ORIGINS,
    OUTLIER_MIN_N,
    REALTIME_CROSSCHECK,
    REALTIME_REQUEST_DELAY_SEC,
    ROUNDTRIP_VS_ONEWAY_MEDIAN_RATIO,
    TRIPS,
    USE_HISTORICAL_BASELINE,
)

DEALS_JSON = Path(__file__).resolve().parent.parent / "deals.json"
REQUEST_DELAY_SEC = 0.5


def scrub_secret(text, secret):
    return text.replace(secret, "***") if secret else text


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


def judge(stats, history, threshold):
    """Compute discount of current min vs baseline and flag deals against the
    route's threshold. Baseline is fixed to bootstrap (current cross-sectional
    median); the historical mode is only used when USE_HISTORICAL_BASELINE is
    enabled and enough days exist."""
    if USE_HISTORICAL_BASELINE and len(history) >= MIN_HISTORY_DAYS:
        baseline = statistics.median(history)
        basis = f"historical ({len(history)}d)"
    else:
        baseline = stats["median"]
        basis = f"bootstrap (history {len(history)}d)"
    discount = (baseline - stats["min"]) / baseline * 100
    return baseline, discount, discount >= threshold, basis


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
                except Exception:
                    latest = []
                finally:
                    time.sleep(REQUEST_DELAY_SEC)

                items = filter_items(items, latest, now)
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

                history = dealdb.historical_mins(conn, origin, dest, trip_label, today)
                threshold = deal_threshold(dest)
                baseline, discount, is_deal, basis = judge(stats, history, threshold)
                results.append({
                    "origin": origin, "dest": dest, "trip": trip_label, "status": "ok",
                    "min": stats["min"], "median": stats["median"], "n": stats["n"],
                    "baseline": baseline, "discount": discount, "threshold": threshold,
                    "is_deal": is_deal, "basis": basis, "cheap": cheap,
                })

    conn.close()
    drop_impossible_roundtrips(results)
    crosscheck_realtime(results)
    write_deals_json(results)
    report(results, today)


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
        "threshold_pct": DEAL_THRESHOLD_PCT,
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
                "threshold_pct": r["threshold"],
                "departure_at": r["cheap"].get("departure_at"),
                "return_at": r["cheap"].get("return_at") or None,
                "airline": r["cheap"].get("airline"),
                "gate": r["cheap"].get("gate"),
                "cache_date": cache_date_from_link(r["cheap"].get("link")),
                "link": strip_link_params(r["cheap"].get("link")),
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
    print(f"# Snapshot {today}  (threshold: -{DEAL_THRESHOLD_PCT:.0f}%)\n")
    header = f"{'Route':<10}{'Trip':<11}{'Min':>10}{'Baseline':>11}{'Disc':>8}  {'Deal':<4} Basis"
    print(header)
    print("-" * len(header))

    for r in results:
        route = f"{r['origin']}->{r['dest']}"
        if r["status"] != "ok":
            print(f"{route:<10}{r['trip']:<11}{'-':>10}{'-':>11}{'-':>8}  {'-':<4} {r['status']}")
            continue
        mark = "DROP" if r.get("sanity_note") else ("YES" if r["is_deal"] else "no")
        basis = r.get("sanity_note") or f"{r['basis']}  thr-{r['threshold']:.0f}%"
        print(
            f"{route:<10}{r['trip']:<11}{r['min']:>10,}{round(r['baseline']):>11,}"
            f"{r['discount']:>7.1f}%  {mark:<4} {basis}"
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
            f"- {r['origin']}->{r['dest']} [{r['trip']}] {r['min']:,} KRW "
            f"(baseline {round(r['baseline']):,}, -{r['discount']:.1f}%) "
            f"출발 {(c.get('departure_at') or '')[:10]}{ret} "
            f"{c.get('airline', '')}/{c.get('gate', '')}"
        )


if __name__ == "__main__":
    main()
