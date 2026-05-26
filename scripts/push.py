"""
Supabase + Expo Push I/O for the per-user trigger.

Three responsibilities:
- load_active_users()         : pull subscribers with alarm_master=true
- load_recent_sent_per_user() : pull recent (token,route) sends for dedup
- send_pushes() / record_sent(): fan out via Expo Push, persist to push_history

Secrets read from env only (per project policy):
  SUPABASE_URL, SUPABASE_SERVICE_KEY (server key — bypasses RLS)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
EXPO_BATCH_SIZE = 100      # Expo's documented per-request cap
EXPO_TIMEOUT = 15
RETRY_DELAYS = (1, 3, 8)   # exponential backoff for transient Expo failures


def _env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


# ---------- Supabase REST ----------

def _supabase_get(path: str, params: dict | None = None) -> list:
    url = f"{_env('SUPABASE_URL').rstrip('/')}/rest/v1/{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    req = urllib.request.Request(url, headers={
        "apikey": _env("SUPABASE_SERVICE_KEY"),
        "Authorization": f"Bearer {_env('SUPABASE_SERVICE_KEY')}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _supabase_post(path: str, rows: list[dict]) -> None:
    if not rows:
        return
    url = f"{_env('SUPABASE_URL').rstrip('/')}/rest/v1/{path}"
    body = json.dumps(rows).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "apikey": _env("SUPABASE_SERVICE_KEY"),
        "Authorization": f"Bearer {_env('SUPABASE_SERVICE_KEY')}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def _supabase_patch(path_with_filter: str, payload: dict) -> None:
    """PATCH a filtered set of rows. path_with_filter already includes the
    PostgREST filter (e.g. 'push_tokens?token=in.(...)')."""
    url = f"{_env('SUPABASE_URL').rstrip('/')}/rest/v1/{path_with_filter}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="PATCH", headers={
        "apikey": _env("SUPABASE_SERVICE_KEY"),
        "Authorization": f"Bearer {_env('SUPABASE_SERVICE_KEY')}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def _parse_total(content_range: str) -> int:
    """PostgREST returns 'Content-Range: 0-N/total' (or '*/total'). Extract total."""
    if not content_range or "/" not in content_range:
        return -1
    try:
        return int(content_range.rsplit("/", 1)[1])
    except ValueError:
        return -1


def _supabase_count(path_with_filter: str) -> int:
    """Count rows matching `path_with_filter` without pulling the data.
    Uses Range: 0-0 + Prefer: count=exact so PostgREST reports total via
    Content-Range. Returns -1 if the server didn't include a count."""
    url = f"{_env('SUPABASE_URL').rstrip('/')}/rest/v1/{path_with_filter}"
    req = urllib.request.Request(url, headers={
        "apikey": _env("SUPABASE_SERVICE_KEY"),
        "Authorization": f"Bearer {_env('SUPABASE_SERVICE_KEY')}",
        "Accept": "application/json",
        "Prefer": "count=exact",
        "Range-Unit": "items",
        "Range": "0-0",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            cr = resp.headers.get("Content-Range", "")
            resp.read()
    except urllib.error.HTTPError as e:
        # Empty result set or out-of-range still includes Content-Range (416).
        if e.code in (206, 416):
            cr = e.headers.get("Content-Range", "")
        else:
            raise
    return _parse_total(cr)


def _supabase_delete(path_with_filter: str) -> int:
    """DELETE rows matching the filter. Returns the deleted count (via
    Content-Range with Prefer: count=exact), or -1 if not reported."""
    url = f"{_env('SUPABASE_URL').rstrip('/')}/rest/v1/{path_with_filter}"
    req = urllib.request.Request(url, method="DELETE", headers={
        "apikey": _env("SUPABASE_SERVICE_KEY"),
        "Authorization": f"Bearer {_env('SUPABASE_SERVICE_KEY')}",
        "Prefer": "count=exact",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        cr = resp.headers.get("Content-Range", "")
        resp.read()
    return _parse_total(cr)


# ---------- Active users ----------

def load_active_users() -> list[dict]:
    """All users with alarm_master=true. Returned dicts include all settings
    needed by trigger._matches_user. Dev/fake tokens are excluded server-side
    so they don't cause Expo errors."""
    rows = _supabase_get(
        "push_tokens",
        {
            "select": "token,origins,destinations,alarm_master,alarm_window,"
                      "disc_short_pct,disc_long_pct,lang",
            "alarm_master": "eq.true",
            "token": "not.like.*DEV-*",   # skip dev fake tokens
        },
    )
    return rows or []


# ---------- Per-user sent history ----------

def load_recent_sent_per_user(days: int) -> set[str]:
    """Returns set of 'token|route_key' strings sent within the last `days`.
    Caller uses set membership for O(1) dedup lookup."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    rows = _supabase_get(
        "push_history",
        {"select": "token,route_key", "sent_at": f"gte.{since}"},
    )
    return {f"{r['token']}|{r['route_key']}" for r in (rows or []) if r.get("token")}


# ---------- Expo Push ----------

def _post_expo_batch(messages: list[dict]) -> dict:
    """One HTTP call to Expo with exponential-backoff retries."""
    body = json.dumps(messages).encode("utf-8")
    last_err: Exception | None = None
    for attempt, delay in enumerate(RETRY_DELAYS):
        try:
            req = urllib.request.Request(
                EXPO_PUSH_URL, data=body, method="POST",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=EXPO_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < len(RETRY_DELAYS) - 1:
                time.sleep(delay)
    raise RuntimeError(f"Expo push failed after {len(RETRY_DELAYS)} attempts: {last_err}")


def _format_notification(deal: dict, lang: str = "ko") -> tuple[str, str]:
    """Push title/body. Keep ko default; en is a thin fallback for non-ko users.
    Pure so it could be unit-tested if you ever want to lock the wording."""
    pct = round(float(deal["discount_pct"]))
    price_man = round(deal["price"] / 10000)  # 만원 — natural for Korean users
    if lang == "en":
        trip_tag = "Round-trip" if deal["trip"] == "roundtrip" else "One-way"
        title = f"✈️ {deal['from']} → {deal['destination']} {pct}% off"
        body = f"{trip_tag} ₩{price_man}0,000 · {deal.get('airline', '')}"
    else:
        trip_tag = "왕복" if deal["trip"] == "roundtrip" else "편도"
        title = f"✈️ {deal['from']} → {deal['destination']} {pct}% 할인"
        body = f"{trip_tag} {price_man}만원 · {deal.get('airline', '')}"
    return title, body


def send_pushes(push_plan: list[tuple]) -> list[dict]:
    """Fan out pushes. push_plan: list of (token, deal, reason, lang).
    Returns list of {token, deal} actually sent (for record_sent)."""
    if not push_plan:
        return []

    messages: list[dict] = []
    sent_records: list[dict] = []

    for token, deal, _, lang in push_plan:
        title, body = _format_notification(deal, lang=lang)
        messages.append({
            "to": token,
            "title": title,
            "body": body,
            "sound": "default",
            "priority": "high",
            "data": {
                "route_key": f"{deal['from']}|{deal['destination']}|{deal['trip']}",
                "link": deal.get("link"),
                "discount_pct": deal["discount_pct"],
            },
        })
        sent_records.append({"token": token, "deal": deal})

    # Chunk and send. Collect tokens flagged DeviceNotRegistered for one-shot
    # deactivation after the loop (Expo's data[] preserves request order).
    expired: list[str] = []
    for i in range(0, len(messages), EXPO_BATCH_SIZE):
        batch = messages[i:i + EXPO_BATCH_SIZE]
        try:
            resp = _post_expo_batch(batch)
        except Exception as e:
            # One batch fail shouldn't kill all the others.
            print(f"WARN: Expo batch failed: {e}")
            continue
        data = resp.get("data") or []
        # Expo's contract is data[i] ↔ batch[i] in order, same length. If a
        # partial-failure ever returns a short/long array, the index→token
        # mapping is unreliable — skip rather than misclassify a live token.
        if len(data) != len(batch):
            print(
                f"WARN: Expo response length {len(data)} != batch {len(batch)}; "
                "skipping DeviceNotRegistered scan for this batch"
            )
            continue
        for j, item in enumerate(data):
            if (item.get("status") == "error"
                    and (item.get("details") or {}).get("error") == "DeviceNotRegistered"):
                expired.append(batch[j]["to"])

    if expired:
        # A token can appear multiple times in one run (multiple deals); dedupe
        # before the UPDATE so the in.() list stays small.
        try:
            deactivate_tokens(sorted(set(expired)))
        except Exception as e:
            print(f"WARN: deactivate_tokens failed: {e}")

    return sent_records


DEACTIVATE_CHUNK = 100  # Cap PATCH ?in.() list size; URL ~100 chars/token encoded.


def deactivate_tokens(tokens: list[str]) -> None:
    """Soft-disable expired Expo tokens by flipping alarm_master=false.
    Soft instead of DELETE so the row survives if the user re-opens the app
    (the client will refresh the token and re-enable on the next subscribe).
    Expo's DeviceNotRegistered is the only error we treat as permanent here;
    other errors (rate limit, message too big, sender mismatch) stay transient.

    Chunked at DEACTIVATE_CHUNK because mass-expiry events (OS update, bulk
    sign-out) can produce hundreds of tokens in one run — a single ?in.() URL
    would blow past PostgREST's URL-length limit and silently drop the lot."""
    if not tokens:
        return
    total = 0
    for i in range(0, len(tokens), DEACTIVATE_CHUNK):
        chunk = tokens[i:i + DEACTIVATE_CHUNK]
        # PostgREST `in.()` value list: URL-encode each token so '[' ']' from
        # ExponentPushToken[...] don't trip the filter parser.
        encoded = ",".join(urllib.parse.quote(t, safe="") for t in chunk)
        try:
            _supabase_patch(f"push_tokens?token=in.({encoded})", {"alarm_master": False})
            total += len(chunk)
        except Exception as e:
            # Don't let one bad chunk lose the rest.
            print(f"WARN: deactivate chunk {i // DEACTIVATE_CHUNK} failed: {e}")
    print(f"Deactivated {total}/{len(tokens)} expired tokens")


def record_sent(sent_records: list[dict], now: dt.datetime) -> None:
    """Append rows to push_history. Per-user records (token + route_key) so
    dedup is precise per subscriber."""
    if not sent_records:
        return
    rows = [{
        "token": s["token"],
        "route_key": f"{s['deal']['from']}|{s['deal']['destination']}|{s['deal']['trip']}",
        "sent_at": now.isoformat(),
        "discount_pct": s["deal"].get("discount_pct"),
        "price": s["deal"].get("price"),
    } for s in sent_records]
    _supabase_post("push_history", rows)
    print(f"Recorded {len(rows)} sends to push_history.")


def cleanup_push_history(days: int, dry_run: bool = False) -> int:
    """Delete push_history rows with sent_at older than `days`. Returns the
    row count (deleted, or — in dry_run — would-be-deleted). dry_run path
    issues no writes."""
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    # PostgREST filter values must be URL-encoded (timestamp has ':' and '+').
    filt = f"push_history?sent_at=lt.{urllib.parse.quote(cutoff, safe='')}"
    if dry_run:
        return _supabase_count(filt)
    return _supabase_delete(filt)
