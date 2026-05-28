ORIGINS = ["ICN", "GMP", "PUS", "TAE", "CJU"]
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

# --- Deal judgment (Step 3: "near floor") ---
# A deal = current min is at or below the route's recent price floor
# +DEAL_CAP_PCT. The floor (baseline) is the mean of the 5 lowest daily-minimums
# within a rolling window. Deals below the floor get the "green" highlight tier
# (the standout deal, cheaper than the recent low); deals from the floor up to
# +DEAL_CAP_PCT are regular deals with no tier (tier=None) — shown in the app
# without a color label. Above floor +DEAL_CAP_PCT it's not a deal.
DEAL_CAP_PCT = 20.0

# Baseline is computed only from daily-mins within this rolling window (days
# back from today). Keeps the floor on the current season — older data stays
# in the DB for backtest/audit but is excluded from the baseline so that
# off-season prices don't distort it. If fewer days exist (e.g. 7 so far),
# use what's there.
BASELINE_WINDOW_DAYS = 30

# Baseline = mean of the 5 lowest daily minimums within BASELINE_WINDOW_DAYS.
# Below MIN_HISTORY_DAYS days of data we can't trust the floor — history guard
# blocks the alert.
MIN_HISTORY_DAYS = 5

# Cabin-mix protector (Step 2-A-0). The Travelpayouts API doesn't expose
# cabin class and trip_class=0 is partially ignored by the cache, so a
# fraction of business/first fares (3-10x economy) can sneak in. Drop the
# top X% by price as a distribution-shape-agnostic guard — works even when
# contaminating fares are the majority (median-based filters fail in that
# case because the median itself is inflated). Heuristic: tune in Step 2 if
# observed in production.
OUTLIER_DROP_TOP_PCT = 0.30

# Sanity guard (Step 2-A-4). Even in the near-floor model, a min more than this
# far BELOW the floor is almost always residual contamination, not a real
# fare. is_deal=False + WARN log when fired.
SANITY_MAX_DISCOUNT_PCT = 50.0

# Publish safety (Step 3 hardening). If more than this fraction of routes error
# out (dead token, API outage) or zero deals result, snapshot aborts BEFORE
# writing deals.json so the workflow's commit/mirror skip and the last good feed
# is preserved instead of being overwritten with an empty/garbage one. Only
# data-API fetch failures count toward the rate; realtime-crosscheck failures
# keep the candidate and never set an error status, so they don't inflate it.
MAX_ERROR_RATE = 0.5

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
# regardless of age. Kept tight (2d) because cache age is the main driver of the
# "price went up at booking" gap; 1d would starve the feed (some routes don't
# refresh daily) and risk the 0-deals publish guard.
MAX_CACHE_AGE_DAYS = 2

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
# stale/unbookable enough that we don't trust it at all. Smaller gaps aren't
# dropped; instead apply_conservative_pricing anchors the published price on the
# live fare (see DISPLAY_SAFETY_BUFFER_PCT). Any failure (scrape error/timeout,
# FX lookup, missing dependency) keeps the candidate so a flaky check never
# empties the feed.
REALTIME_CROSSCHECK = True
MAX_PRICE_DIVERGENCE_PCT = 20.0
REALTIME_REQUEST_DELAY_SEC = 1.0

# Conservative display pricing. The number we publish (and re-judge the deal on)
# is the higher of the cached cheapest fare and the live cross-check fare, plus
# this buffer, rounded up to the nearest 1,000 KRW. Goal: the price shown is
# almost always >= what the user actually pays at booking, so clicking through
# surprises downward (cheaper), never upward. The buffer also covers routes
# where the live cross-check is unavailable (scrape down) — there the cached
# fare alone gets the buffer. Trade-off: a higher shown price means fewer/less
# flashy deals, accepted on purpose for trust.
DISPLAY_SAFETY_BUFFER_PCT = 7.0
