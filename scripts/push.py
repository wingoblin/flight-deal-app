"""
Push delivery layer — sends notifications via Expo Push, persists send
history to Supabase.

Why split from trigger.py:
- trigger.py is pure (decisions in, decisions out — easy to test).
- push.py owns I/O: HTTP, env vars, retries, persistence.

Two pieces of state in Supabase:
  push_tokens(token, created_at, ...)        -- registered devices
  push_history(route_key, sent_at, ...)      -- dedup ledger

Secrets read from env only (per project policy — no hardcoded keys, never
log them). Required:
  SUPABASE_URL, SUPABASE_SERVICE_KEY   (server-side key, never ship to app)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request
from typing import Iterable

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
EXPO_BATCH_SIZE = 100  # Expo's documented per-request cap
EXPO_TIMEOUT = 15      # seconds — Expo is usually <1s, generous for cold DNS

# Single-attempt retry budget. Exponential backoff to avoid hammering Expo
# when they're degraded (policy: external calls get retry+backoff).
RETRY_DELAYS = (1, 3, 8)


def _env(name: str) -> str:
    """Required env var or raise — fail loud, never silent-skip."""
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


# ---------- Supabase REST (no SDK dependency) ----------

def _supabase_get(path: str, params: dict | None = None) -> list:
    """GET against Supabase PostgREST. Returns parsed JSON (list of rows)."""
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
    """Bulk insert — server-side key bypasses RLS for write paths we control."""
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


# ---------- Public API used by trigger.py ----------

def load_recent_sent(days: int) -> dict[str, str]:
    """Returns {route_key: latest_sent_at_iso} for pushes within last `days`.
    Used by the dedup gate. Server-side filter keeps payload small even when
    history grows large."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    rows = _supabase_get(
        "push_history",
        {"select": "route_key,sent_at", "sent_at": f"gte.{since}",
         "order": "sent_at.desc"},
    )
    # Many rows per route possible; first one wins because of order.desc.
    out: dict[str, str] = {}
    for r in rows:
        out.setdefault(r["route_key"], r["sent_at"])
    return out


def _load_active_tokens() -> list[str]:
    rows = _supabase_get("push_tokens", {"select": "token"})
    return [r["token"] for r in rows if r.get("token")]


def _post_expo_batch(messages: list[dict]) -> dict:
    """One HTTP call to Expo with exponential-backoff retries.
    Returns the parsed response so caller can inspect per-message errors."""
    body = json.dumps(messages).encode("utf-8")
    last_err: Exception | None = None
    for attempt, delay in enumerate(RETRY_DELAYS):
        try:
            req = urllib.request.Request(EXPO_PUSH_URL, data=body, method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=EXPO_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < len(RETRY_DELAYS) - 1:
                time.sleep(delay)
    raise RuntimeError(f"Expo push failed after {len(RETRY_DELAYS)} attempts: {last_err}")


def send_pushes(decisions) -> list[dict]:
    """Fan out one notification per fired decision to all active tokens.

    Returns the list of (route_key, deal) actually sent so the caller can
    record them. We persist on a per-route basis (not per-token) because the
    dedup gate is route-scoped, not user-scoped — every user gets the same
    deals."""
    from trigger import _route_key, format_notification  # late import to avoid cycle

    tokens = _load_active_tokens()
    if not tokens:
        print("No registered push tokens — skipping send.")
        return []

    sent_records: list[dict] = []
    for decision in decisions:
        deal = decision.deal
        title, body = format_notification(deal)
        route_key = "|".join(_route_key(deal))

        # Build one Expo message per token (Expo requires explicit recipients).
        messages = [{
            "to": tok,
            "title": title,
            "body": body,
            "sound": "default",
            "priority": "high",
            "data": {
                "route_key": route_key,
                "link": deal.get("link"),
                "discount_pct": deal["discount_pct"],
            },
        } for tok in tokens]

        # Chunk by Expo's 100/req limit.
        for i in range(0, len(messages), EXPO_BATCH_SIZE):
            batch = messages[i:i + EXPO_BATCH_SIZE]
            try:
                _post_expo_batch(batch)
            except Exception as e:
                # Don't abort other routes on one batch failure; log and continue.
                print(f"WARN: batch for {route_key} failed: {e}")
                continue

        sent_records.append({"route_key": route_key, "deal": deal})

    return sent_records


def record_sent(sent_records: list[dict], now: dt.datetime) -> None:
    """Append rows to push_history. Idempotent at the route level — if the
    same row gets inserted twice in one run (shouldn't happen but defensive),
    the dedup gate next time still works because we read MAX(sent_at)."""
    if not sent_records:
        return
    rows = [{
        "route_key": s["route_key"],
        "sent_at": now.isoformat(),
        "discount_pct": s["deal"].get("discount_pct"),
        "price": s["deal"].get("price"),
    } for s in sent_records]
    _supabase_post("push_history", rows)
    print(f"Recorded {len(rows)} sends to push_history.")
