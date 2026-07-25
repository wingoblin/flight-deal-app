#!/usr/bin/env python3
"""Post newly-surfaced hot flight deals to a Telegram channel.

Reads deals.json (produced by snapshot.py), picks the best deals that have NOT
already been sent within a rolling time window (default 24h), renders them as
card images that mirror the airpick web UI, and posts them to a Telegram
channel. If image rendering is unavailable, it falls back to a clean text card
so an alert is never dropped.

Runs entirely on GitHub Actions — no dependency on the website being up. The
only network calls are to api.telegram.org.

Secrets / env:
  TELEGRAM_BOT_TOKEN   Bot token from @BotFather (required to actually send)
  TELEGRAM_CHAT_ID     Channel id, e.g. "@airpick" or "-100..." (required)
  TELEGRAM_MIN_DISCOUNT   Minimum discount_pct to consider (default 0)
  TELEGRAM_MAX_PRICE      Only surface round-trips at/under this KRW price (default none)
  TELEGRAM_PRICE_OVERRIDE_DISCOUNT  Discount_pct at/above which the price ceiling
                                    is ignored (catch long-haul steals; default none)
  TELEGRAM_ALWAYS_UNDER_PRICE  Round-trips at/under this KRW price are ALWAYS surfaced,
                               regardless of discount_pct (catch flat-out cheap fares;
                               default none)
  TELEGRAM_MAX_CARDS      Max deals per run (default 6)
  TELEGRAM_WINDOW_HOURS   Don't resend a deal seen within this many hours (default 24)
  TELEGRAM_STATE_FILE     Where the "already sent" state lives (default data/telegram_sent.json)
  TELEGRAM_RANK           "discount" (default) or "price"
  DRY_RUN                 "true" -> build/render but do NOT send

Local preview (no telegram, no playwright needed):
  python scripts/telegram_notify.py --dump-html /tmp/cards.html
"""

import json
import os
import sys
import time
import uuid
import datetime as dt
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEALS_JSON = ROOT / "deals.json"
META_JSON = Path(__file__).resolve().parent / "telegram_meta.json"

KST = dt.timezone(dt.timedelta(hours=9))


# ----------------------------- helpers ---------------------------------------

def env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def fmt_krw(n):
    try:
        return f"{int(round(float(n))):,}"
    except (TypeError, ValueError):
        return str(n)


def date_only(iso):
    return (iso or "")[:10]


def parse_date(iso):
    return dt.date.fromisoformat(date_only(iso))


def today_kst():
    override = env("TELEGRAM_NOW")  # ISO date, for testing
    if override:
        return dt.date.fromisoformat(override[:10])
    return dt.datetime.now(KST).date()


def d_day(dep_iso, base=None):
    base = base or today_kst()
    return (parse_date(dep_iso) - base).days


def deal_key(d):
    """Identity of a deal, independent of small price wiggles."""
    return "|".join([
        str(d.get("from", "")),
        str(d.get("destination", "")),
        str(d.get("trip", "")),
        date_only(d.get("departure_at")),
        date_only(d.get("return_at")),
        str(d.get("airline", "")),
    ])


# ----------------------------- selection -------------------------------------

def _deal_price(d):
    try:
        return float(d.get("price", 1e18))
    except (TypeError, ValueError):
        return None


def _is_always(d, always_under_price):
    """A flat-out cheap fare that pings regardless of discount %."""
    if always_under_price is None:
        return False
    price = _deal_price(d)
    return price is not None and price <= always_under_price


def select_deals(deals, state, now, min_discount, max_cards, window_hours, rank,
                 max_price=None, price_override_discount=None, always_under_price=None):
    window = dt.timedelta(hours=window_hours)
    sent = state.get("sent", {})
    candidates = []
    for d in deals:
        if d.get("trip") != "roundtrip":  # round-trip only
            continue
        disc = float(d.get("discount_pct", 0))
        always = _is_always(d, always_under_price)
        # A flat-out cheap fare (at/under ALWAYS_UNDER_PRICE) skips both the
        # discount gate and the price ceiling — it's worth surfacing on price
        # alone. Everything else must clear the usual discount + price gates.
        if not always:
            if disc < min_discount:
                continue
            # Price ceiling — but a big enough drop (e.g. a long-haul half-off
            # steal) overrides it, since that's a deal worth surfacing anyway.
            if max_price is not None and not (
                price_override_discount is not None and disc >= price_override_discount
            ):
                price = _deal_price(d)
                if price is None or price > max_price:
                    continue
        try:
            if d_day(d.get("departure_at")) < 0:  # already departed
                continue
        except ValueError:
            continue
        key = deal_key(d)
        last = sent.get(key)
        if last:
            try:
                last_dt = dt.datetime.fromisoformat(last)
                if now - last_dt < window:
                    continue  # sent recently — skip
            except ValueError:
                pass
        candidates.append(d)

    if rank == "price":
        candidates.sort(key=lambda d: (d.get("price", 1e18), -float(d.get("discount_pct", 0))))
    else:  # discount
        candidates.sort(key=lambda d: (-float(d.get("discount_pct", 0)), d.get("price", 1e18)))

    # Guarantee the "always" cheap fares get first dibs on the limited card
    # slots so a low discount % never bumps them out of the run.
    always_deals = [d for d in candidates if _is_always(d, always_under_price)]
    other_deals = [d for d in candidates if not _is_always(d, always_under_price)]
    return (always_deals + other_deals)[:max_cards]


def prune_state(state, now, window_hours):
    keep_after = now - dt.timedelta(hours=window_hours * 2)
    sent = state.get("sent", {})
    pruned = {}
    for k, v in sent.items():
        try:
            if dt.datetime.fromisoformat(v) >= keep_after:
                pruned[k] = v
        except ValueError:
            continue
    state["sent"] = pruned
    return state


# ----------------------------- rendering -------------------------------------

_BADGE_COLORS = [
    "#0b5cff", "#15a673", "#ff8a00", "#ff3b30", "#7b61ff",
    "#00a3a3", "#e0399a", "#3a4350", "#0090d4", "#c2410c",
]


def _badge_color(code):
    return _BADGE_COLORS[sum(ord(c) for c in str(code)) % len(_BADGE_COLORS)]


CARD_CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
body {
  font-family:'Noto Sans CJK KR','Noto Sans KR','Apple SD Gothic Neo',sans-serif;
  background:#eef0f3;
  padding:13px;
  width:360px;
  -webkit-font-smoothing:antialiased;
}
.card {
  background:#fff; border-radius:13px; padding:11px 13px;
  box-shadow:0 1px 2px rgba(15,23,42,.05), 0 6px 16px rgba(15,23,42,.07);
  margin-bottom:8px;
}
.card:last-child { margin-bottom:0; }
.top { display:flex; align-items:flex-start; justify-content:space-between; }
.left { display:flex; gap:9px; align-items:center; min-width:0; }
.badge {
  width:33px; height:33px; border-radius:10px; flex:0 0 auto;
  display:flex; align-items:center; justify-content:center;
  color:#fff; font-weight:800; font-size:11px; letter-spacing:-.3px;
}
.name-row { display:flex; align-items:center; gap:5px; }
.city { font-size:15.5px; font-weight:800; color:#0c1116; letter-spacing:-.4px; }
.country { font-size:11px; color:#8b95a3; font-weight:600; }
.code {
  font-size:10px; color:#6b7480; font-weight:700; background:#f0f2f5;
  padding:1px 6px; border-radius:5px; letter-spacing:.3px;
}
.route { font-size:11.5px; color:#8b95a3; font-weight:600; margin-top:2px; }
.dday {
  font-size:11.5px; font-weight:800; color:#3a4350; background:#f0f2f5;
  padding:3px 9px; border-radius:999px; white-space:nowrap;
}
.divider { height:1px; background:#eef0f2; margin:10px 0; }
.bottom { display:flex; align-items:flex-end; justify-content:space-between; }
.dates { font-size:12.5px; font-weight:500; color:#0c1116; letter-spacing:-.2px; }
.meta { font-size:11px; color:#8b95a3; font-weight:600; margin-top:3px; }
.price { font-size:18px; font-weight:800; color:#0c1116; letter-spacing:-.6px; text-align:right; }
.low { font-size:10.5px; color:#8b95a3; font-weight:600; text-align:right; margin-top:2px; }
"""


def card_fields(d, meta):
    code = d.get("destination", "")
    m = meta.get("dests", {}).get(code, {})
    origins = meta.get("origins", {})
    airlines = meta.get("airlines", {})
    ac = d.get("airline", "")
    dep = date_only(d.get("departure_at"))
    ret = date_only(d.get("return_at"))
    is_oneway = (d.get("trip") != "roundtrip") or (not ret) or (ret == dep)

    a = parse_date(dep)
    if is_oneway:
        date_txt = f"{a.year}.{a.month:02d}.{a.day:02d}"
        nights_txt = "편도"
    else:
        b = parse_date(ret)
        nights = (b - a).days
        date_txt = f"{a.year}.{a.month:02d}.{a.day:02d} ⇄ {b.month:02d}.{b.day:02d}"
        nights_txt = f"{nights}박{nights + 1}일"

    stops = int(d.get("transfers", 0) or 0)
    stop_txt = "직항" if stops == 0 else f"경유 {stops}회"
    cabin = d.get("cabin_class", "")
    cabin_txt = "이코노미" if cabin == "economy" else (cabin or "")
    meta_bits = " · ".join(x for x in [nights_txt, stop_txt, cabin_txt] if x)

    return {
        "city": m.get("ko", code),
        "country": m.get("country_ko", ""),
        "code": code,
        "origin": origins.get(d.get("from", ""), d.get("from", "")),
        "airline_mark": ac,
        "badge_color": _badge_color(ac),
        "airline_name": airlines.get(ac, ac),
        "arrow": "→" if is_oneway else "⇄",
        "dday": d_day(dep),
        "date_txt": date_txt,
        "meta_bits": meta_bits,
        "price": fmt_krw(d.get("price")),
        "baseline": fmt_krw(d.get("baseline")) if d.get("baseline") else "",
        "link": d.get("link", ""),
    }


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_card_html(f):
    low = f'<div class="low">최근 최저가 ₩{esc(f["baseline"])}</div>' if f["baseline"] else ""
    return f"""
    <div class="card">
      <div class="top">
        <div class="left">
          <div class="badge" style="background:{f['badge_color']}">{esc(f['airline_mark'])}</div>
          <div class="txt">
            <div class="name-row">
              <span class="city">{esc(f['city'])}</span>
              <span class="country">{esc(f['country'])}</span>
              <span class="code">{esc(f['code'])}</span>
            </div>
            <div class="route">{esc(f['origin'])} {f['arrow']} {esc(f['city'])}</div>
          </div>
        </div>
        <div class="dday">D-{f['dday']}</div>
      </div>
      <div class="divider"></div>
      <div class="bottom">
        <div class="bl">
          <div class="dates">{esc(f['date_txt'])}</div>
          <div class="meta">{esc(f['meta_bits'])}</div>
        </div>
        <div class="br">
          <div class="price">{esc(f['price'])}원~</div>
          {low}
        </div>
      </div>
    </div>
    """


def build_page_html(fields_list):
    cards = "\n".join(build_card_html(f) for f in fields_list)
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{CARD_CSS}</style></head><body>{cards}</body></html>"


def render_png(html, out_path):
    """Render HTML to a PNG. Returns True on success, False if playwright/browser
    is unavailable (caller then uses the text fallback)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[render] playwright not installed — text fallback")
        return False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"])
            # Render at 3x so the 440px card is ~1320px wide — above phone
            # screen width, so Telegram doesn't upscale (and blur) it.
            page = browser.new_page(viewport={"width": 440, "height": 200}, device_scale_factor=3)
            page.set_content(html, wait_until="networkidle")
            # clip tightly to the body box so no empty vertical space remains
            page.locator("body").screenshot(path=str(out_path))
            browser.close()
        return True
    except Exception as e:  # noqa: BLE001 - any render failure -> fallback
        print(f"[render] failed ({e}) — text fallback")
        return False


# ----------------------------- telegram --------------------------------------

TG_API = "https://api.telegram.org"


def _tg_call(token, method, fields, files=None):
    url = f"{TG_API}/bot{token}/{method}"
    if not files:
        data = json.dumps(fields).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    else:
        boundary = uuid.uuid4().hex
        body = bytearray()
        for k, v in fields.items():
            body += f"--{boundary}\r\n".encode()
            body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
            body += f"{v}\r\n".encode()
        for k, (fname, content, ctype) in files.items():
            body += f"--{boundary}\r\n".encode()
            body += f'Content-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'.encode()
            body += f"Content-Type: {ctype}\r\n\r\n".encode()
            body += content + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=bytes(body),
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


# App-store links shown under every alert so readers can jump to the markets.
APP_STORE_URL = "https://apps.apple.com/kr/app/id6781919421"
PLAY_STORE_URL = "https://play.google.com/store/apps/details?id=com.airpick.app"


def app_links_keyboard():
    return [[
        {"text": "아이폰", "url": APP_STORE_URL},
        {"text": "안드로이드", "url": PLAY_STORE_URL},
    ]]


def send_photo(token, chat_id, png_bytes, caption, keyboard=None):
    fields = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
    if keyboard:
        fields["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    files = {"photo": ("deals.png", png_bytes, "image/png")}
    return _tg_call(token, "sendPhoto", fields, files)


def send_message(token, chat_id, text, keyboard=None):
    fields = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True}
    if keyboard:
        fields["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    return _tg_call(token, "sendMessage", fields)


def text_fallback(fields_list):
    lines = ["✈️ <b>실시간 특가알람</b>", ""]
    for i, f in enumerate(fields_list, 1):
        low = f"  ·  최근최저가 ₩{f['baseline']}" if f["baseline"] else ""
        lines.append(
            f"<b>{i}. {esc(f['city'])} ({esc(f['code'])})</b>  D-{f['dday']}\n"
            f"   {esc(f['origin'])} {f['arrow']} {esc(f['city'])} · {esc(f['meta_bits'])}\n"
            f"   <b>{esc(f['price'])}원~</b>{low}"
        )
    return "\n".join(lines)


# ----------------------------- main ------------------------------------------

def main():
    args = sys.argv[1:]
    dump_html = None
    if "--dump-html" in args:
        dump_html = args[args.index("--dump-html") + 1]

    data = load_json(DEALS_JSON, {})
    deals = data.get("deals", [])
    meta = load_json(META_JSON, {"dests": {}, "airlines": {}, "origins": {}})

    min_discount = float(env("TELEGRAM_MIN_DISCOUNT", "0"))
    max_cards = int(env("TELEGRAM_MAX_CARDS", "6"))
    window_hours = float(env("TELEGRAM_WINDOW_HOURS", "24"))
    rank = env("TELEGRAM_RANK", "discount")
    max_price = env("TELEGRAM_MAX_PRICE")
    max_price = float(max_price) if max_price else None
    price_override_discount = env("TELEGRAM_PRICE_OVERRIDE_DISCOUNT")
    price_override_discount = float(price_override_discount) if price_override_discount else None
    always_under_price = env("TELEGRAM_ALWAYS_UNDER_PRICE")
    always_under_price = float(always_under_price) if always_under_price else None
    state_file = Path(env("TELEGRAM_STATE_FILE", str(ROOT / "data" / "telegram_sent.json")))
    dry_run = env("DRY_RUN", "false").lower() == "true"

    now = dt.datetime.now(dt.timezone.utc)
    state = load_json(state_file, {"version": 1, "sent": {}})

    picked = select_deals(deals, state, now, min_discount, max_cards, window_hours, rank,
                          max_price=max_price, price_override_discount=price_override_discount,
                          always_under_price=always_under_price)
    fields_list = [card_fields(d, meta) for d in picked]

    if dump_html is not None:
        Path(dump_html).write_text(build_page_html(fields_list), encoding="utf-8")
        print(f"[dump] {len(fields_list)} card(s) -> {dump_html}")
        for f in fields_list:
            print(f"  - {f['city']}({f['code']}) {f['price']}원 D-{f['dday']}")
        return 0

    if not picked:
        print("[telegram] no new hot deals to send")
        return 0

    print(f"[telegram] {len(picked)} deal(s) selected:")
    for f in fields_list:
        print(f"  - {f['city']}({f['code']}) {f['price']}원 D-{f['dday']}")

    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping send "
              "(configure repo secrets to enable).")
        return 0

    caption = "✈️ <b>실시간 특가알람</b>"
    keyboard = app_links_keyboard()

    png = ROOT / "telegram_cards.png"
    rendered = False
    if not dry_run:
        rendered = render_png(build_page_html(fields_list), png)
    elif render_png(build_page_html(fields_list), png):
        rendered = True

    if dry_run:
        print(f"[telegram] DRY_RUN — would send {len(fields_list)} deal(s) "
              f"({'image' if rendered else 'text'}). Not sending.")
        return 0

    try:
        if rendered:
            resp = send_photo(token, chat_id, png.read_bytes(), caption, keyboard)
        else:
            resp = send_message(token, chat_id, text_fallback(fields_list), keyboard)
        if not resp.get("ok"):
            print(f"[telegram] API error: {resp}")
            return 1
    except urllib.error.HTTPError as e:
        print(f"[telegram] HTTP {e.code}: {e.read().decode('utf-8', 'replace')}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"[telegram] send failed: {e}")
        return 1

    # mark as sent + persist
    iso = now.isoformat()
    for d in picked:
        state.setdefault("sent", {})[deal_key(d)] = iso
    prune_state(state, now, window_hours)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"[telegram] sent {len(picked)} deal(s); state -> {state_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
