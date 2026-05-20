import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"


def get_token():
    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        raise SystemExit("TRAVELPAYOUTS_TOKEN env var required")
    return token


def fetch_prices(origin, destination, one_way, token, currency="krw", limit=1000,
                 retries=3, backoff=2.0):
    params = {
        "origin": origin,
        "destination": destination,
        "currency": currency,
        "one_way": "true" if one_way else "false",
        "unique": "false",
        "sorting": "price",
        "limit": limit,
        "token": token,
    }
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    delay = backoff
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                body = json.loads(resp.read())
            return body.get("data") or []
        except urllib.error.HTTPError as e:
            # Retry rate limits and transient server errors; fail fast otherwise.
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
