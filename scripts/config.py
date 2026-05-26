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

# Single deal threshold applied uniformly to all routes (Step 2-A-3 decision).
# DEAL_THRESHOLD_PCT_BY_DEST stays as an empty dict so future per-route tuning
# can add entries without code changes; deal_threshold() falls back to default.
# 2단계에서 노선별 override 추가 가능 — 1단계는 단일값.
DEAL_THRESHOLD_PCT_DEFAULT = 15.0

DEAL_THRESHOLD_PCT_BY_DEST = {}


def deal_threshold(dest):
    return DEAL_THRESHOLD_PCT_BY_DEST.get(dest, DEAL_THRESHOLD_PCT_DEFAULT)


# Baseline = mean of the 5 lowest historical daily minimums for the route
# (Step 2-A-2). Below this we don't have enough days to trust the floor —
# block the alert via the history guard. cycle-by-cycle cross-sectional
# median (old "bootstrap" mode) was removed: cabin contamination made it
# unreliable.
MIN_HISTORY_DAYS = 5

# Cabin-mix protector (Step 2-A-0). The Travelpayouts API doesn't expose
# cabin class and trip_class=0 is partially ignored by the cache, so a
# fraction of business/first fares (3-10x economy) can sneak in. Drop the
# top X% by price as a distribution-shape-agnostic guard — works even when
# contaminating fares are the majority (median-based filters fail in that
# case because the median itself is inflated). Heuristic: tune in Step 2 if
# observed in production.
OUTLIER_DROP_TOP_PCT = 0.30

# Sanity guard (Step 2-A-4). Normal economy deals don't exceed this discount;
# anything above is almost always residual baseline contamination despite
# other guards. is_deal=False + WARN log when fired.
SANITY_MAX_DISCOUNT_PCT = 50.0

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

# --- Push notification trigger (second-stage cut, on top of deal display cut) ---
# deals.json holds everything past the 25%/15% DISPLAY cut. These extras decide
# which of those also earn a push notification. Tunable from one place, no code
# changes needed elsewhere.
# --- Push notification trigger ---
# Per-user filtering: each subscriber's discount cuts (single short-haul,
# single long-haul) and origin/destination filters come from their
# Supabase push_tokens row. No global cut applies on top.
# Only the dedup window stays global (idempotency across all users).
PUSH_DEDUP_DAYS = 3                   # same (token, from, dest, trip) → 1 push per 3d

# Keep push_history rows this long for audit/debug. Comfortably longer than
# PUSH_DEDUP_DAYS so raising the dedup window later doesn't require touching
# this. trigger.py prunes anything older at the end of each cycle.
PUSH_HISTORY_RETENTION_DAYS = 30
