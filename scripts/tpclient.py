import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
LATEST_URL = "https://api.travelpayouts.com/aviasales/v3/get_latest_prices"


def get_token():
    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        raise SystemExit("TRAVELPAYOUTS_TOKEN env var required")
    return token


def _get_data(url, token, retries, backoff):
    # Token goes in a header, never the URL, so it can't leak into logs or errors.
    req = urllib.request.Request(url, headers={"X-Access-Token": token})
    delay = backoff
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
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
        "trip_class": 0,    # economy only (0=economy, 1=business per TP docs)
    }
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    return _get_data(url, token, retries, backoff)


def fetch_latest(origin, destination, one_way, token, currency="krw", limit=1000,
                 retries=3, backoff=2.0):
    """get_latest_prices mirrors prices_for_dates fare-for-fare but exposes the
    freshness fields (actual, found_at) that prices_for_dates omits, so it's used
    only to validate freshness -- it has no booking link."""
    params = {
        "origin": origin,
        "destination": destination,
        "currency": currency,
        "one_way": "true" if one_way else "false",
        "limit": limit,
        "trip_class": 0,    # mirror fetch_prices so freshness keys align
    }
    url = f"{LATEST_URL}?{urllib.parse.urlencode(params)}"
    return _get_data(url, token, retries, backoff)
