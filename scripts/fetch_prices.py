"""
Travelpayouts Aviasales Data API: ICN 출발 캐시 최저가 조회.

엔드포인트: GET https://api.travelpayouts.com/v2/prices/latest
캐시 데이터(최근 48시간 검색 기반). period_type=year로 1년치 캐시 풀에서 조회.
"""
import os
import sys
import requests

API_URL = "https://api.travelpayouts.com/v2/prices/latest"
ORIGIN = "ICN"
DESTINATIONS = [
    ("도쿄", "TYO"),
    ("오사카", "OSA"),
    ("방콕", "BKK"),
    ("다낭", "DAD"),
    ("세부", "CEB"),
    ("타이베이", "TPE"),
]
CURRENCY = "krw"
LIMIT = 30


def fetch_route(token: str, dest_code: str) -> list[dict]:
    params = {
        "origin": ORIGIN,
        "destination": dest_code,
        "currency": CURRENCY,
        "period_type": "year",
        "page": 1,
        "limit": LIMIT,
        "show_to_affiliates": "true",
        "sorting": "price",
        "trip_class": 0,
        "token": token,
    }
    r = requests.get(API_URL, params=params, timeout=15)
    r.raise_for_status()
    body = r.json()
    if not body.get("success"):
        raise RuntimeError(f"API error: {body.get('error')}")
    return body.get("data") or []


def format_row(item: dict) -> str:
    price = item.get("value")
    depart = item.get("depart_date", "?")
    transfers = item.get("number_of_changes", 0)
    stop = "직항" if transfers == 0 else f"경유{transfers}"
    found = item.get("found_at", "?")
    return f"  {price:>10,} KRW | 출발 {depart} | {stop} | 캐시시점 {found}"


def main() -> int:
    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        print("환경변수 TRAVELPAYOUTS_TOKEN 미설정", file=sys.stderr)
        return 1

    for name, code in DESTINATIONS:
        print(f"\n[{name} ({code})]")
        try:
            data = fetch_route(token, code)
        except Exception as e:
            print(f"  요청 실패: {e}")
            continue

        if not data:
            print("  데이터 없음")
            continue

        rows = sorted(data, key=lambda x: x.get("value", float("inf")))
        for item in rows:
            print(format_row(item))

    return 0


if __name__ == "__main__":
    sys.exit(main())
