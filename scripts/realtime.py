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


def usd_to_krw(timeout=15):
    """Live USD->KRW rate. Raises on network/parse failure (caller decides)."""
    with urllib.request.urlopen(FX_URL, timeout=timeout) as resp:
        return json.load(resp)["rates"]["KRW"]


def cheapest_krw(origin, dest, depart_date, return_date, fx):
    """Cheapest live economy fare (KRW) for the route/dates, or None if no price.

    fast-flights is imported lazily so a missing dependency surfaces as a normal
    exception at call time rather than breaking module import."""
    from fast_flights import FlightData, Passengers, get_flights

    legs = [FlightData(date=depart_date, from_airport=origin, to_airport=dest)]
    trip = "one-way"
    if return_date:
        legs.append(FlightData(date=return_date, from_airport=dest, to_airport=origin))
        trip = "round-trip"

    res = get_flights(
        flight_data=legs,
        trip=trip,
        seat="economy",
        passengers=Passengers(adults=1, children=0, infants_in_seat=0, infants_on_lap=0),
        fetch_mode="fallback",
    )
    prices = []
    for f in res.flights:
        m = re.search(r"(\d[\d,]*)", str(getattr(f, "price", "")))
        if m:
            prices.append(int(m.group(1).replace(",", "")))
    return round(min(prices) * fx) if prices else None
