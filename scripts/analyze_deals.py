import json
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN")
if not TOKEN:
    sys.exit("TRAVELPAYOUTS_TOKEN env var required")

ORIGIN = "ICN"
DESTINATIONS = ["TYO", "OSA", "BKK", "DAD", "CEB", "TPE"]
BASE = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"


def fetch_distribution(dest: str, one_way: bool):
    params = {
        "origin": ORIGIN,
        "destination": dest,
        "currency": "krw",
        "one_way": "true" if one_way else "false",
        "unique": "false",
        "sorting": "price",
        "limit": 1000,
        "token": TOKEN,
    }
    url = f"{BASE}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read())


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


print(
    f"{'Route':<10}{'Trip':<11}{'N':>5}{'Min':>10}{'P25':>10}"
    f"{'Median':>10}{'Mean':>10}{'vs Median':>11}{'vs Mean':>10}"
)
print("-" * 87)

details = []
for dest in DESTINATIONS:
    for one_way in (True, False):
        label = "oneway" if one_way else "roundtrip"
        route = f"{ORIGIN}->{dest}"
        try:
            body = fetch_distribution(dest, one_way)
        except urllib.error.HTTPError as e:
            print(f"{route:<10}{label:<11}HTTP {e.code}")
            continue
        except Exception as e:
            print(f"{route:<10}{label:<11}ERROR {e!r}")
            continue

        items = body.get("data") or []
        stats = summarize(items)
        if not stats:
            print(f"{route:<10}{label:<11}  no data")
            continue

        vs_median = (stats["median"] - stats["min"]) / stats["median"] * 100
        vs_mean = (stats["mean"] - stats["min"]) / stats["mean"] * 100
        print(
            f"{route:<10}{label:<11}{stats['n']:>5}"
            f"{stats['min']:>10,}{stats['p25']:>10,.0f}"
            f"{stats['median']:>10,.0f}{stats['mean']:>10,}"
            f"{vs_median:>10.1f}%{vs_mean:>9.1f}%"
        )
        details.append(
            {
                "route": route,
                "trip": label,
                "stats": stats,
                "vs_median_pct": round(vs_median, 1),
                "vs_mean_pct": round(vs_mean, 1),
                "cheapest": cheapest_item(items),
            }
        )

print()
print("=== Cheapest sample per (route, trip) ===")
for d in details:
    print(f"\n{d['route']} [{d['trip']}]  vs median: {d['vs_median_pct']}%  vs mean: {d['vs_mean_pct']}%")
    print(json.dumps(d["cheapest"], indent=2, ensure_ascii=False))
