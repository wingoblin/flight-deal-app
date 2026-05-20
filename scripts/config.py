ORIGIN = "ICN"
DESTINATIONS = ["TYO", "OSA", "BKK", "DAD", "CEB", "TPE"]
TRIPS = [("oneway", True), ("roundtrip", False)]

# A deal is flagged when the current minimum sits at least this far below the
# median baseline (whole-route distribution, no date/trip-duration grouping).
DEAL_THRESHOLD_PCT = 25.0

# Baseline is the current cross-sectional median until this many prior daily
# snapshots exist, after which it switches to the median of past daily minimums.
MIN_HISTORY_DAYS = 14
