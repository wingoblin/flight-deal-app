"""ICN 출발 주요 노선의 캐시 최저가 조회 (Travelpayouts Aviasales API v3)."""

import json
import os
import sys
import urllib.parse
import urllib.request

ORIGIN = "ICN"
DESTINATIONS = ["TYO", "OSA", "BKK", "DAD", "CEB", "TPE"]
ENDPOINT = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"


def fetch(origin: str, destination: str, token: str) -> dict:
    params = {
        "origin": origin,
        "destination": destination,
        "currency": "krw",
        "sorting": "price",
        "direct": "false",
        "limit": 1,
        "token": token,
    }
    url = f"{ENDPOINT}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        print("TRAVELPAYOUTS_TOKEN not set", file=sys.stderr)
        return 1

    for dest in DESTINATIONS:
        try:
            payload = fetch(ORIGIN, dest, token)
        except Exception as e:
            print(f"{ORIGIN} -> {dest}: ERROR {e}")
            continue
        print(f"=== {ORIGIN} -> {dest} ===")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
