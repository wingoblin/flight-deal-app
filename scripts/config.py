ORIGIN = "ICN"
DESTINATIONS = [
    # Japan — major
    "TYO", "OSA", "FUK", "NGO", "OKA",
    # Japan — regional (Korean direct, mostly LCC)
    "TOY", "KMI", "KCZ", "KMQ", "TAK", "KOJ", "OIT", "KMJ", "HSG",
    "AOJ", "MYJ", "HIJ", "FSZ",
    # Southeast Asia / resort
    "BKK", "DAD", "HAN", "SGN", "CEB", "MNL", "SIN", "KUL", "CGK", "DPS",
    "KLO", "USM", "HKT", "KBV", "PNH", "SAI", "CNX", "CXR", "PQC", "VTE",
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
