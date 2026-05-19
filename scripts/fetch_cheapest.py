import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN")
if not TOKEN:
    sys.exit("TRAVELPAYOUTS_TOKEN env var required")

ORIGIN = "ICN"
DESTINATIONS = ["TYO", "OSA", "BKK", "DAD", "CEB", "TPE"]
BASE_URL = "https://api.travelpayouts.com/aviasales/v3/get_latest_prices"


def fetch(dest: str, one_way: bool):
    params = {
        "origin": ORIGIN,
        "destination": dest,
        "currency": "krw",
        "one_way": "true" if one_way else "false",
        "limit": 1,
        "token": TOKEN,
    }
    url = f"{BASE_URL}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')}"}
    except Exception as e:
        return {"error": repr(e)}
    return body


rows = []
for dest in DESTINATIONS:
    for one_way in (True, False):
        body = fetch(dest, one_way)
        items = body.get("data") or []
        if not items:
            rows.append(
                {
                    "destination": dest,
                    "trip": "oneway" if one_way else "roundtrip",
                    "depart_date": None,
                    "raw": body,
                }
            )
            continue
        item = items[0]
        rows.append(
            {
                "destination": dest,
                "trip": "oneway" if one_way else "roundtrip",
                "depart_date": item.get("depart_date"),
                "raw": item,
            }
        )

rows.sort(key=lambda r: (r["depart_date"] is None, r["depart_date"] or ""))

for r in rows:
    print(f"=== {ORIGIN} -> {r['destination']} [{r['trip']}] ===")
    print(json.dumps(r["raw"], indent=2, ensure_ascii=False))
    print()
