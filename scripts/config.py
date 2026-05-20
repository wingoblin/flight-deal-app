ORIGIN = "ICN"
DESTINATIONS = [
    # Japan — major
    "TYO", "OSA", "FUK", "NGO", "OKA",
    # Japan — regional (Korean direct, mostly LCC)
    "KMQ", "TAK", "KOJ", "KMJ", "MYJ", "HIJ", "FSZ",
    # Southeast Asia / resort
    "BKK", "DAD", "HAN", "SGN", "CEB", "MNL", "SIN", "KUL", "CGK", "DPS",
    "KLO", "USM", "HKT", "KBV", "SAI", "CNX", "CXR", "PQC", "VTE",
    "BKI",
    # East Asia
    "TPE", "HKG",
    # Europe
    "CDG", "LHR", "FCO", "BCN", "FRA", "AMS", "IST",
    "ZRH", "VIE", "MUC", "PRG", "MAD", "HEL",
    # Americas
    "JFK", "LAX", "HNL", "YVR", "SEA", "ATL", "DFW", "IAD", "LAS", "YYZ",
    # Pacific resort
    "GUM", "SPN",
    # Oceania
    "SYD",
]
TRIPS = [("oneway", True), ("roundtrip", False)]

# A deal is flagged when the current minimum sits at least this far below the
# median baseline (whole-route distribution, no date/trip-duration grouping).
DEAL_THRESHOLD_PCT = 25.0

# Per-destination overrides for the deal threshold. Long-haul routes (Europe,
# Americas) swing less in percentage terms, so a smaller discount already counts
# as a deal. Destinations not listed fall back to DEAL_THRESHOLD_PCT.
DEAL_THRESHOLD_PCT_BY_DEST = {
    d: 15.0 for d in (
        # Europe
        "CDG", "LHR", "FCO", "BCN", "FRA", "AMS", "IST",
        "ZRH", "VIE", "MUC", "PRG", "MAD", "HEL",
        # Americas
        "JFK", "LAX", "HNL", "YVR", "SEA", "ATL", "DFW", "IAD", "LAS", "YYZ",
    )
}


def deal_threshold(dest):
    return DEAL_THRESHOLD_PCT_BY_DEST.get(dest, DEAL_THRESHOLD_PCT)

# Judgment baseline is fixed to bootstrap mode (the current cache's
# cross-sectional median). Snapshots keep accumulating either way, so when
# enough history exists this can be flipped on to compare/switch to the median
# of past daily minimums after MIN_HISTORY_DAYS.
USE_HISTORICAL_BASELINE = False
MIN_HISTORY_DAYS = 14

# Error guard: a roundtrip can't realistically cost less than a single one-way
# leg, so flag (and exclude from alerts) any roundtrip priced below its route's
# cheapest one-way, or below this fraction of the one-way median -- both are
# clear cache errors. One-way fares are never touched, so genuine deep deals are
# preserved. The median check only applies when the one-way side has enough
# samples for the median to be meaningful.
ROUNDTRIP_VS_ONEWAY_MEDIAN_RATIO = 0.65
OUTLIER_MIN_N = 20

# Cache freshness: drop any fare last found (found_at, cross-checked via
# get_latest_prices) at least this many days ago; staler cache diverges further
# from the real, bookable price. Fares the seller marks actual=false are dropped
# regardless of age.
MAX_CACHE_AGE_DAYS = 3

# Low-trust gates (sellers): fares from these are dropped before judging so they
# never become a deal or set the baseline. Keep results to trustworthy gates
# (e.g. Trip.com, City.Travel, Kiwi.com, Mytrip.com). Tune as needed.
BLOCKED_GATES = {
    "Авиасейлс",
    "Farera",
    "Biletix",
    "Clickavia",
    "Tickets",
}

# Departure validity: drop fares departing in less than this many hours. Too
# little lead time means it's effectively unbookable by the time the alert lands.
MIN_HOURS_BEFORE_DEPARTURE = 24

# Real-time cross-check (fast-flights / Google Flights): drop a deal candidate
# whose live cheapest fare exceeds the Travelpayouts price by at least
# MAX_PRICE_DIVERGENCE_PCT percent -- a large gap means the cached fare is
# likely stale/unbookable. Any failure (scrape error/timeout, FX lookup, missing
# dependency) keeps the candidate so a flaky check never empties the feed.
REALTIME_CROSSCHECK = True
MAX_PRICE_DIVERGENCE_PCT = 30.0
REALTIME_REQUEST_DELAY_SEC = 1.0
