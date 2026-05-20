ORIGIN = "ICN"
DESTINATIONS = [
    # Asia
    "TYO", "OSA", "FUK", "NGO", "OKA", "BKK", "DAD", "HAN", "SGN",
    "CEB", "MNL", "TPE", "HKG", "SIN", "KUL", "CGK", "DPS",
    # Europe
    "CDG", "LHR", "FCO", "BCN", "FRA", "AMS", "IST",
    # Americas
    "JFK", "LAX", "HNL", "YVR",
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
