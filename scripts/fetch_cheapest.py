import json
import os
import sys
import urllib.parse
import urllib.request

TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN")
if not TOKEN:
    sys.exit("TRAVELPAYOUTS_TOKEN env var required")

ORIGIN = "ICN"
DESTINATIONS = ["TYO", "OSA", "BKK", "DAD", "CEB", "TPE"]
BASE_URL = "https://api.travelpayouts.com/aviasales/v3/get_latest_prices"

for dest in DESTINATIONS:
    params = {
        "origin": ORIGIN,
        "destination": dest,
        "currency": "krw",
        "limit": 1,
        "token": TOKEN,
    }
    url = f"{BASE_URL}?{urllib.parse.urlencode(params)}"
    print(f"=== {ORIGIN} -> {dest} ===")
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read())
        print(json.dumps(data, indent=2, ensure_ascii=False))
    except urllib.error.HTTPError as e:
        print(f"HTTPError {e.code}: {e.read().decode('utf-8', 'replace')}")
    except Exception as e:
        print(f"Error: {e!r}")
    print()
