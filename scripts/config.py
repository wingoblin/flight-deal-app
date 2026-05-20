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

# Baseline is the current cross-sectional median until this many prior daily
# snapshots exist, after which it switches to the median of past daily minimums.
MIN_HISTORY_DAYS = 14

# Outlier guard: a minimum sitting this far below the route's 10th percentile
# (P10) is detached from the legit-cheap cluster, so treat it as a cache error
# and exclude it from alerts. Only applied when the route has >= OUTLIER_MIN_N
# samples so P10 is meaningful.
OUTLIER_P10_DISCOUNT_PCT = 50.0
OUTLIER_MIN_N = 10
