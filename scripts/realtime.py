"""Real-time price cross-check via fast-flights (Google Flights).

Used to drop Travelpayouts deal candidates whose cached price sits far below the
live price -- a sign the cached fare is stale or no longer bookable. fast-flights
scrapes Google Flights, so it can be slow or fail; callers must treat any
exception here as "could not verify" and keep the candidate.
"""
import json
import re
import urllib.request

FX_URL = "https://open.er-api.com/v6/latest/USD"

# Verification fetch modes tried in order. "local" drives a real Chromium via
# Playwright (installed in the workflow) — it renders the page so it gets past
# the consent walls / bot checks that block the lighter modes, so it's the most
# reliable. "fallback" (serverless) is the backup if the browser is unavailable
# or errors. We take the first mode that returns any price; if all fail we return
# None and the caller treats the candidate as unverified. Order matters: most
# reliable first so we don't spend a flaky result when a good one is available.
FETCH_MODES = ("local", "fallback")


def usd_to_krw(timeout=15):
    """Live USD->KRW rate. Raises on network/parse failure (caller decides)."""
    with urllib.request.urlopen(FX_URL, timeout=timeout) as resp:
        return json.load(resp)["rates"]["KRW"]


def _prices_from(res):
    """Extract numeric KRW-less prices from a fast-flights result."""
    prices = []
    for f in getattr(res, "flights", []) or []:
        m = re.search(r"(\d[\d,]*)", str(getattr(f, "price", "")))
        if m:
            prices.append(int(m.group(1).replace(",", "")))
    return prices


def cheapest_krw(origin, dest, depart_date, return_date, fx):
    """Cheapest live economy fare (KRW) for the route/dates, or None if no price.

    Tries FETCH_MODES in order (browser-based 'local' first, 'fallback' second)
    and returns the first mode that yields a price, so a single flaky mode no
    longer means "unverified". fast-flights is imported lazily so a missing
    dependency surfaces as a normal exception at call time rather than breaking
    module import."""
    from fast_flights import FlightData, Passengers, get_flights

    legs = [FlightData(date=depart_date, from_airport=origin, to_airport=dest)]
    trip = "one-way"
    if return_date:
        legs.append(FlightData(date=return_date, from_airport=dest, to_airport=origin))
        trip = "round-trip"
    passengers = Passengers(adults=1, children=0, infants_in_seat=0, infants_on_lap=0)

    for mode in FETCH_MODES:
        try:
            res = get_flights(
                flight_data=legs,
                trip=trip,
                seat="economy",
                passengers=passengers,
                fetch_mode=mode,
            )
        except Exception:
            continue   # this mode failed (browser missing, scrape error) -> try next
        prices = _prices_from(res)
        if prices:
            return round(min(prices) * fx)
    return None

