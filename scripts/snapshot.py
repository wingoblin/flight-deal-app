import datetime as dt
import json
import statistics
import time
from pathlib import Path

import dealdb
import tpclient
from config import (
    DEAL_THRESHOLD_PCT,
    DESTINATIONS,
    MIN_HISTORY_DAYS,
    ORIGIN,
    OUTLIER_MIN_N,
    ROUNDTRIP_VS_ONEWAY_MEDIAN_RATIO,
    TRIPS,
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


def judge(stats, history):
    """Pick baseline (historical median once enough days, else current median),
    compute discount of current min vs baseline, and flag deals."""
    if len(history) >= MIN_HISTORY_DAYS:
        baseline = statistics.median(history)
        basis = f"historical ({len(history)}d)"
    else:
        baseline = stats["median"]
        basis = f"current ({len(history)}/{MIN_HISTORY_DAYS}d)"
    discount = (baseline - stats["min"]) / baseline * 100
    return baseline, discount, discount >= DEAL_THRESHOLD_PCT, basis


def main():
    token = tpclient.get_token()
    today = dt.date.today().isoformat()
    conn = dealdb.connect()

    results = []
    for dest in DESTINATIONS:
        for trip_label, one_way in TRIPS:
            try:
                items = tpclient.fetch_prices(ORIGIN, dest, one_way, token)
            except Exception as e:
                msg = scrub_secret(repr(e), token)
                results.append({"dest": dest, "trip": trip_label, "status": f"error: {msg}"})
                continue
            finally:
                time.sleep(REQUEST_DELAY_SEC)

            stats = summarize(items)
            if not stats:
                results.append({"dest": dest, "trip": trip_label, "status": "no-data"})
                continue

            cheap = cheapest_item(items)
            dealdb.upsert_snapshot(conn, {
                "snapshot_date": today,
                "origin": ORIGIN,
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

            history = dealdb.historical_mins(conn, ORIGIN, dest, trip_label, today)
            baseline, discount, is_deal, basis = judge(stats, history)
            results.append({
                "dest": dest, "trip": trip_label, "status": "ok",
                "min": stats["min"], "median": stats["median"], "n": stats["n"],
                "baseline": baseline, "discount": discount,
                "is_deal": is_deal, "basis": basis, "cheap": cheap,
            })

    conn.close()
    drop_impossible_roundtrips(results)
    write_deals_json(results)
    report(results, today)


def select_deals(results):
    return [r for r in results if r.get("status") == "ok" and r["is_deal"]]


def write_deals_json(results):
    deals = select_deals(results)
    deals.sort(key=lambda r: r["discount"], reverse=True)
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "origin": ORIGIN,
        "currency": "KRW",
        "threshold_pct": DEAL_THRESHOLD_PCT,
        "deals": [
            {
                "destination": r["dest"],
                "trip": r["trip"],
                "price": r["min"],
                "baseline": round(r["baseline"]),
                "discount_pct": round(r["discount"], 1),
                "departure_at": r["cheap"].get("departure_at"),
                "return_at": r["cheap"].get("return_at") or None,
                "airline": r["cheap"].get("airline"),
                "gate": r["cheap"].get("gate"),
                "link": r["cheap"].get("link"),
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
        r["dest"]: r
        for r in results
        if r["status"] == "ok" and r["trip"] == "oneway"
    }
    for r in results:
        if r.get("status") != "ok" or r["trip"] != "roundtrip":
            continue
        ow = oneway.get(r["dest"])
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
        route = f"{ORIGIN}->{r['dest']}"
        if r["status"] != "ok":
            print(f"{route:<10}{r['trip']:<11}{'-':>10}{'-':>11}{'-':>8}  {'-':<4} {r['status']}")
            continue
        mark = "DROP" if r.get("sanity_note") else ("YES" if r["is_deal"] else "no")
        basis = r.get("sanity_note") or r["basis"]
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
            f"- {ORIGIN}->{r['dest']} [{r['trip']}] {r['min']:,} KRW "
            f"(baseline {round(r['baseline']):,}, -{r['discount']:.1f}%) "
            f"출발 {(c.get('departure_at') or '')[:10]}{ret} "
            f"{c.get('airline', '')}/{c.get('gate', '')}"
        )


if __name__ == "__main__":
    main()
