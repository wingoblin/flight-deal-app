ORIGINS = ["ICN", "GMP", "PUS", "TAE", "CJU", "CJJ", "MWX"]
DESTINATIONS = [
    # Japan — major
    "TYO", "OSA", "FUK", "NGO", "OKA", "CTS",
    # Japan — regional (Korean direct, mostly LCC)
    "KMQ", "TAK", "KOJ", "KMJ", "MYJ", "HIJ", "FSZ",
    "SDJ", "KIJ", "KKJ", "KMI", "OIT",
    # China
    "PVG", "PEK", "TAO", "DLC", "SHE", "WEH", "HRB", "YNJ", "CAN", "XMN",
    # Southeast Asia / resort
    "BKK", "DAD", "HAN", "SGN", "CEB", "MNL", "SIN", "KUL", "CGK", "DPS",
    "KLO", "USM", "HKT", "KBV", "SAI", "CNX", "CXR", "PQC", "VTE",
    "BKI", "PNH", "RGN", "PEN", "LGK", "SUB", "CRK",
    # East Asia
    "TPE", "HKG", "KHH", "MFM",
    # Middle East / South Asia
    "DXB", "DOH", "DEL",
    # Europe
    "CDG", "LHR", "FCO", "BCN", "FRA", "AMS", "IST",
    "ZRH", "VIE", "MUC", "PRG", "MAD", "HEL",
    "LIS", "ATH", "CPH", "ARN", "WAW", "VCE", "MXP",
    # Americas
    "JFK", "LAX", "HNL", "YVR", "SEA", "ATL", "DFW", "IAD", "LAS", "YYZ",
    "SFO", "ORD",
    # Pacific resort
    "GUM", "SPN",
    # Oceania
    "SYD", "AKL", "MEL", "BNE",
]
TRIPS = [("oneway", True), ("roundtrip", False)]

# --- Deal judgment (Step 3: "below typical") ---
# A deal = current min is at least DEAL_THRESHOLD_PCT *below* the route's typical
# price. The baseline is the MEDIAN of the daily minimums in a rolling window —
# what the route's cheapest fare normally is, not its all-time floor. Comparing
# to the floor (and allowing a band *above* it) made almost every normal price
# qualify and the shown "discount" come out near zero or negative; comparing to
# the typical price and requiring a real discount below it surfaces only
# genuinely cheap days and lets the app show an honest "N% below usual". All
# deals are shown the same way (no color tiers).
DEAL_THRESHOLD_PCT = 0.0

# Baseline is computed only from daily-mins within this rolling window (days
# back from today). Keeps the baseline on the current season — older data stays
# in the DB for backtest/audit but is excluded so that off-season prices don't
# distort it. If fewer days exist (e.g. 7 so far), use what's there.
BASELINE_WINDOW_DAYS = 30

# Baseline = median of the daily minimums within BASELINE_WINDOW_DAYS.
# Below MIN_HISTORY_DAYS days of data we can't trust the baseline — history guard
# blocks the alert. Kept low (3) so sparse routes (regional airports like TAE/CJU
# with only a handful of cached fares) can still build a usable baseline.
MIN_HISTORY_DAYS = 3

# Minimum number of fares today (after the outlier filter) to judge a deal.
# Below this the sample is too thin to trust, so the deal is blocked (today_n
# guard). Lowered to 3 to let thin-coverage routes (e.g. 대구/TAE) surface;
# the sanity/roundtrip-vs-oneway/cache-age/blocked-gate guards still protect
# against the bad single fares that thin samples are prone to.
MIN_TODAY_FARES = 3

# Regional (low-coverage) origins — 부산/대구/제주/청주/무안. Travelpayouts barely
# caches these, so the strict guards above starve them and only ICN/GMP ever
# surface. Relax both the history and today-fare guards for these origins only;
# major hubs keep the strict values.
#
# Business-class noise: the DISPLAYED price is always the cheapest fare, which
# is economy unless an entire day cached only business — so the price the user
# sees stays clean. The baseline is the median of daily mins, which is robust to
# a single business-contaminated day even on a thin route (a high outlier doesn't
# move the median, unlike a mean). The per-day top-OUTLIER_DROP_TOP_PCT cabin-mix
# filter also still runs on every route, and the sanity guard
# (SANITY_MAX_DISCOUNT_PCT) blocks any residual implausible discount.
REGIONAL_ORIGINS = {"PUS", "TAE", "CJU", "CJJ", "MWX"}
REGIONAL_MIN_HISTORY_DAYS = 2
REGIONAL_MIN_TODAY_FARES = 2


def guards_for_origin(origin):
    """(min_history_days, min_today_fares) for an origin: relaxed for regional
    origins, strict for major hubs (ICN/GMP)."""
    if origin in REGIONAL_ORIGINS:
        return REGIONAL_MIN_HISTORY_DAYS, REGIONAL_MIN_TODAY_FARES
    return MIN_HISTORY_DAYS, MIN_TODAY_FARES

# Cabin-mix protector (Step 2-A-0). The Travelpayouts API doesn't expose
# cabin class and trip_class=0 is partially ignored by the cache, so a
# fraction of business/first fares (3-10x economy) can sneak in. Drop the
# top X% by price as a distribution-shape-agnostic guard — works even when
# contaminating fares are the majority (median-based filters fail in that
# case because the median itself is inflated). Heuristic: tune in Step 2 if
# observed in production.
OUTLIER_DROP_TOP_PCT = 0.30

# Sanity guard (Step 2-A-4). In the below-typical model, a min more than this
# far BELOW the typical price is almost always residual contamination, not a real
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

# Sparse-route fallback: well-covered routes (ICN/GMP) have plenty of fresh
# fares, but thin-coverage regional routes (대구/TAE, 청주/CJU) are rarely
# searched, so their cache is almost always older than MAX_CACHE_AGE_DAYS and
# gets fully dropped → the route never surfaces. When the strict (fresh) pass
# leaves fewer than MIN_TODAY_FARES fares, we retry allowing cache up to this
# many days old so the route can still appear. The displayed price is still
# re-anchored by the realtime cross-check (which covers regional airports) and
# the +DISPLAY_SAFETY_BUFFER_PCT buffer, so a stale-sourced fare doesn't mislead.
# Set to MAX_CACHE_AGE_DAYS to disable the fallback.
SPARSE_CACHE_AGE_DAYS = 30

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

# Require live verification to publish. The cached cheapest fare can be gone
# (the cheap seat sold out) while the cache still lists it, so a candidate the
# realtime cross-check could NOT confirm (no live price found) is dropped rather
# than published — clicking through such a fare is the main cause of the "price
# jumped at booking" surprise. This only fires per-route when the realtime system
# is up: if fast-flights/FX is entirely unavailable the cross-check returns early
# and every candidate is kept (graceful degradation, feed never emptied by an
# outage). The 0-deals publish guard preserves the last good feed if a bad
# realtime day would otherwise empty it.
REQUIRE_REALTIME_VERIFICATION = True

# Freshness fallback for REQUIRE_REALTIME_VERIFICATION. The live cross-check
# fails for almost all round-trips (fast-flights can't search them), so requiring
# it drops most round-trips. As a softer second signal, keep an unverified
# candidate anyway if its cached fare was found at most this many days ago — a
# fare seen today is much less likely to have sold out than a stale one, so it's
# a reasonable (weaker) stand-in for the live check. Kept tight (1 day) so only
# genuinely fresh fares slip through; the stale fares that actually cause the
# "price jumped" surprise (the regional 30-day cache ones) are still dropped.
# Set to 0 to keep only same-day fares, or negative to disable the fallback
# entirely (back to strict verify-or-drop).
FRESH_VERIFY_MAX_AGE_DAYS = 1

# Conservative display pricing. The published price is the higher of the cached
# cheapest fare and the live cross-check fare, plus a safety buffer, rounded up
# to the nearest 1,000 KRW. Goal: the shown price is almost always >= what the
# user actually pays at booking, so clicking through surprises downward, never
# up. The buffer is adaptive:
#   - LIVE_SAFETY_BUFFER_PCT when the realtime cross-check returned a live price
#     (the displayed price is already anchored on that real, near-bookable fare,
#     so only a small cushion is needed → more competitive).
#   - DISPLAY_SAFETY_BUFFER_PCT when there's no live price (cache only, which can
#     be stale/too-low) → a larger cushion is the only protection.
# Trade-off: a higher shown price means fewer/less flashy deals, accepted for
# trust; the adaptive split keeps prices competitive where we can verify them.
DISPLAY_SAFETY_BUFFER_PCT = 7.0
LIVE_SAFETY_BUFFER_PCT = 3.0
