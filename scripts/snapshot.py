import datetime as dt
import statistics

import dealdb
import tpclient
from config import (
    DEAL_THRESHOLD_PCT,
    DESTINATIONS,
    MIN_HISTORY_DAYS,
    ORIGIN,
    TRIPS,
)


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
                results.append({"dest": dest, "trip": trip_label, "status": f"error: {e!r}"})
                continue

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
                "min": stats["min"], "baseline": baseline, "discount": discount,
                "is_deal": is_deal, "basis": basis, "cheap": cheap,
            })

    conn.close()
    report(results, today)


def report(results, today):
    print(f"# Snapshot {today}  (threshold: -{DEAL_THRESHOLD_PCT:.0f}%)\n")
    header = f"{'Route':<10}{'Trip':<11}{'Min':>10}{'Baseline':>11}{'Disc':>8}  {'Deal':<4} Basis"
    print(header)
    print("-" * len(header))

    deals = []
    for r in results:
        route = f"{ORIGIN}->{r['dest']}"
        if r["status"] != "ok":
            print(f"{route:<10}{r['trip']:<11}{'-':>10}{'-':>11}{'-':>8}  {'-':<4} {r['status']}")
            continue
        mark = "YES" if r["is_deal"] else "no"
        print(
            f"{route:<10}{r['trip']:<11}{r['min']:>10,}{round(r['baseline']):>11,}"
            f"{r['discount']:>7.1f}%  {mark:<4} {r['basis']}"
        )
        if r["is_deal"]:
            deals.append(r)

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
