"""
botapi/api.py — Secure remote API for BotCoin bot server.

Exposes bot data and settings over HTTP on port 8081.
Designed to be called by the remote BotCoin dashboard server.

Security:
  - Every endpoint requires X-Bot-Password header
  - Rate limiting: 5 failed auth attempts = 60s lockout per IP
  - Read-only endpoints: status, trades, health
  - Write endpoints: settings save, manual DCA trigger
  - Kraken API keys are NEVER returned in any response
"""

import sqlite3
import os
import json
import hashlib
import hmac
import time
import threading
import urllib.parse
import urllib.request
import base64
import hashlib as hl
import hmac as hmac_mod
from datetime import datetime, timezone, timedelta
from pathlib import Path
from flask import Flask, jsonify, request, abort, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder="static")

# Allow requests from any origin — dashboard server IP restriction
# is enforced at the firewall level (Vultr firewall rules)
# Restrict CORS to the known dashboard origin — defence in depth on top of firewall
CORS(app, resources={r"/api/*": {"origins": [
    "https://dash.mybotcoin.io",
    "http://localhost",
    "http://127.0.0.1",
]}})

DB_PATH  = Path("/app/data/bot.db")
ENV_PATH = Path("/app/.env")

# ── Rate limiting ─────────────────────────────────────────────────────────────

_failed_attempts = {}   # ip -> {"count": int, "locked_until": float}
_rate_lock = threading.Lock()

MAX_FAILURES   = 5
LOCKOUT_SECS   = 60


def _check_rate_limit(ip: str) -> bool:
    """Return True if IP is allowed, False if locked out."""
    with _rate_lock:
        now = time.time()
        entry = _failed_attempts.get(ip, {"count": 0, "locked_until": 0})
        if now < entry["locked_until"]:
            return False  # still locked
        return True


def _record_failure(ip: str):
    with _rate_lock:
        now = time.time()
        entry = _failed_attempts.get(ip, {"count": 0, "locked_until": 0})
        entry["count"] += 1
        if entry["count"] >= MAX_FAILURES:
            entry["locked_until"] = now + LOCKOUT_SECS
            entry["count"] = 0
        _failed_attempts[ip] = entry


def _record_success(ip: str):
    with _rate_lock:
        _failed_attempts.pop(ip, None)


# ── Auth ──────────────────────────────────────────────────────────────────────

def _read_env() -> dict:
    env = {}
    if not ENV_PATH.exists():
        return env
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def _write_env(updates: dict):
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    written = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        k = stripped.split("=", 1)[0].strip()
        if k in updates:
            new_lines.append(f"{k}={updates[k]}")
            written.add(k)
        else:
            new_lines.append(line)
    for k, v in updates.items():
        if k not in written:
            new_lines.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(new_lines) + "\n")


def _get_password() -> str:
    return _read_env().get("DASHBOARD_PASSWORD", "")


def _check_auth(provided: str) -> bool:
    stored = _get_password()
    if not stored:
        return False
    return hmac.compare_digest(
        hashlib.sha256(provided.encode()).hexdigest(),
        hashlib.sha256(stored.encode()).hexdigest()
    )


def requires_auth(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        ip = request.remote_addr

        if not _check_rate_limit(ip):
            return jsonify({"error": "Too many failed attempts. Try again in 60 seconds."}), 429

        password = (request.headers.get("X-Bot-Password") or
                    (request.get_json(force=True, silent=True) or {}).get("password", ""))

        if not password or not _check_auth(password):
            _record_failure(ip)
            abort(401)

        _record_success(ip)
        return f(*args, **kwargs)
    return decorated


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db(readonly=True):
    """Return a database connection. Read-only by default."""
    if not DB_PATH.exists():
        return None
    if readonly:
        # URI mode — truly read-only, cannot write even by accident
        uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_deposit_tables(conn):
    """
    Create the deposits + btc_price_history tables if they do not yet exist.

    These are normally created by the bot container's database.init_db(), but
    the API container shares the same DB file and may serve /api/deposits before
    the bot has booted on a fresh install. The DDL is kept byte-for-byte in sync
    with bot/database.py so both writers agree on the schema.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS deposits (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            kraken_refid      TEXT UNIQUE NOT NULL,
            currency          TEXT NOT NULL,
            amount            REAL NOT NULL,
            timestamp         TEXT NOT NULL,
            price_usd_at_time REAL,
            usd_value_at_time REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS btc_price_history (
            date            TEXT PRIMARY KEY,
            price_usd       REAL NOT NULL,
            cached_at       DATETIME NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_deposits_timestamp ON deposits(timestamp);
    """)


def _record_deposit(conn, kraken_refid, currency, amount, timestamp,
                    price_usd_at_time, usd_value_at_time):
    """
    Controlled write — insert one deposit row, deduped on Kraken's ledger refid.

    A narrowly-scoped write: no arbitrary SQL, INSERT-only, idempotent via
    ON CONFLICT DO NOTHING so re-syncing the same Kraken ledger never duplicates
    rows. The caller owns the connection so a whole sync batch commits together.
    """
    conn.execute("""
        INSERT INTO deposits
            (kraken_refid, currency, amount, timestamp,
             price_usd_at_time, usd_value_at_time)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(kraken_refid) DO NOTHING
    """, (kraken_refid, currency, round(amount, 8), timestamp,
          price_usd_at_time, round(usd_value_at_time, 2)))


def query(sql, params=()):
    conn = get_db()
    if not conn:
        return []
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def query_one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def _state(key, default=None):
    row = query_one("SELECT value FROM bot_state WHERE key=?", (key,))
    return row["value"] if row else default


# ── Kraken live balance ───────────────────────────────────────────────────────

def _kraken_sign(urlpath, data, secret):
    post_data = urllib.parse.urlencode(data)
    encoded   = (str(data["nonce"]) + post_data).encode()
    message   = urlpath.encode() + hl.sha256(encoded).digest()
    mac       = hmac_mod.new(base64.b64decode(secret), message, hl.sha512)
    return base64.b64encode(mac.digest()).decode()


def _get_live_balances():
    """Fetch live BTC + USD balances from Kraken. Returns dict or None."""
    try:
        env = _read_env()
        api_key    = env.get("KRAKEN_API_KEY", "").strip()
        api_secret = env.get("KRAKEN_API_SECRET", "").strip()
        if not api_key or not api_secret:
            return None

        urlpath = "/0/private/Balance"
        nonce   = str(int(time.time() * 1000))
        data    = {"nonce": nonce}
        post    = urllib.parse.urlencode(data).encode()
        sig     = _kraken_sign(urlpath, data, api_secret)

        req = urllib.request.Request(
            "https://api.kraken.com" + urlpath,
            data=post,
            headers={
                "API-Key":  api_key,
                "API-Sign": sig,
                "Content-Type": "application/x-www-form-urlencoded",
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())

        if body.get("error"):
            return None

        balances = body.get("result", {})
        btc = float(balances.get("XXBT", balances.get("XBT", 0)))
        usd = float(balances.get("ZUSD", balances.get("USD", 0)))
        return {"btc": round(btc, 8), "usd": round(usd, 2)}
    except Exception:
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_live_price() -> float | None:
    """Fetch the current BTC price from Kraken public ticker. No auth required."""
    try:
        req = urllib.request.Request(
            "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
            headers={"User-Agent": "BotCoin/1.0"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
        if body.get("error"):
            return None
        result = body.get("result", {})
        price = float(list(result.values())[0]["c"][0])  # last trade price
        return round(price, 2)
    except Exception:
        return None


def _coingecko_price_at(date_key: str):
    """
    Return BTC/USD price for a UTC day (YYYY-MM-DD), cached in btc_price_history.

    Cache-first: a hit avoids any network call. On a miss we ask CoinGecko's free
    /coins/bitcoin/history endpoint (no auth; expects DD-MM-YYYY) and persist the
    result. A day's price is immutable once past, so the cache is authoritative
    after the first lookup. Returns a float or None on any failure / missing data.
    This mirrors bot/price_history.get_btc_price_at(), kept here because the API
    container is deliberately isolated and does not import the bot package.
    """
    cached = query_one("SELECT price_usd FROM btc_price_history WHERE date=?", (date_key,))
    if cached:
        return cached["price_usd"]

    y, m, d = date_key.split("-")
    params = urllib.parse.urlencode({"date": f"{d}-{m}-{y}", "localization": "false"})
    try:
        req = urllib.request.Request(
            f"https://api.coingecko.com/api/v3/coins/bitcoin/history?{params}",
            headers={"User-Agent": "BotCoin/2.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
        price = body.get("market_data", {}).get("current_price", {}).get("usd")
    except Exception:
        return None
    if price is None:
        return None

    price = float(price)
    conn = get_db(readonly=False)
    if conn:
        try:
            _ensure_deposit_tables(conn)
            conn.execute("""
                INSERT INTO btc_price_history (date, price_usd) VALUES (?, ?)
                ON CONFLICT(date) DO UPDATE SET price_usd=excluded.price_usd
            """, (date_key, price))
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()
    return price


def _fetch_kraken_deposits():
    """
    Pull deposit-type ledger entries (XBT + ZUSD) from Kraken's private Ledgers
    API. Returns a list of normalised dicts {refid, currency, amount, timestamp}
    sorted oldest-first, or [] when keys are missing or the call fails.
    """
    env        = _read_env()
    api_key    = env.get("KRAKEN_API_KEY", "").strip()
    api_secret = env.get("KRAKEN_API_SECRET", "").strip()
    if not api_key or not api_secret:
        return []

    urlpath = "/0/private/Ledgers"
    nonce   = str(int(time.time() * 1000))
    data    = {"nonce": nonce, "type": "deposit"}
    post    = urllib.parse.urlencode(data).encode()
    sig     = _kraken_sign(urlpath, data, api_secret)
    try:
        req = urllib.request.Request(
            "https://api.kraken.com" + urlpath,
            data=post,
            headers={
                "API-Key":  api_key,
                "API-Sign": sig,
                "Content-Type": "application/x-www-form-urlencoded",
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
    except Exception:
        return []

    if body.get("error"):
        return []

    ledger = body.get("result", {}).get("ledger", {})
    deposits = []
    for refid, entry in ledger.items():
        if entry.get("type") != "deposit":
            continue
        asset = entry.get("asset", "")
        if asset in ("XXBT", "XBT"):
            currency = "BTC"
        elif asset in ("ZUSD", "USD"):
            currency = "USD"
        else:
            continue  # ignore non BTC/USD deposits
        try:
            amount = abs(float(entry.get("amount", 0)))
            ts     = float(entry.get("time", 0))
        except (TypeError, ValueError):
            continue
        if amount <= 0:
            continue
        iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        # Kraken keys the ledger dict by refid; entry may also carry "refid"
        # pointing at the funding ref. The dict key is unique per ledger row.
        deposits.append({
            "refid":     refid,
            "currency":  currency,
            "amount":    amount,
            "timestamp": iso,
        })
    deposits.sort(key=lambda d: d["timestamp"])
    return deposits


def _get_200ma():
    rows = query("SELECT price_usd FROM daily_prices ORDER BY date DESC LIMIT 200")
    if len(rows) < 10:
        return None
    return sum(r["price_usd"] for r in rows) / len(rows)


def _trend_duration(ma200):
    """
    Compute how long price has held its current side of the 200-day MA.

    Walks the daily closing prices newest→oldest and counts consecutive days
    that sit on the same side of `ma200` as the most recent day, stopping at the
    first crossover. The returned count is the trend's age in days; if no
    crossover exists in the stored history, the full history length is returned
    (the trend is at least that old). `side` is "above" or "below" relative to
    the 200MA.

    Edge cases, by construction of the loop:
      * Today is itself a crossover day → only today matches → duration 1.
      * No crossover anywhere in history → every day matches → duration == len.
      * A single stored day → duration 1.

    Returns None when there is no daily price data or no computable 200MA, so
    the caller can omit `bot.trend` and let older/empty bots degrade gracefully.
    """
    if not ma200:
        return None
    rows = query("SELECT price_usd FROM daily_prices ORDER BY date DESC")
    if not rows:
        return None

    today_side = "above" if rows[0]["price_usd"] >= ma200 else "below"
    duration = 0
    for r in rows:
        side = "above" if r["price_usd"] >= ma200 else "below"
        if side != today_side:
            break
        duration += 1

    return {"duration_days": duration, "side": today_side}


def _next_dca_timestamp():
    env       = _read_env()
    frequency = env.get("DCA_FREQUENCY", "weekly").lower()
    dca_time  = env.get("DCA_TIME_UTC", "13:00")
    hour, minute = map(int, dca_time.split(":"))
    now = datetime.now(timezone.utc)

    if frequency == "daily":
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if scheduled <= now:
            scheduled += timedelta(days=1)
        return scheduled.isoformat()

    elif frequency == "monthly":
        day_of_month = int(env.get("DCA_DAY_OF_MONTH", "1"))
        # Try this month first
        try:
            candidate = now.replace(day=day_of_month, hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            candidate = now.replace(day=28, hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            # Next month
            if now.month == 12:
                candidate = candidate.replace(year=now.year + 1, month=1)
            else:
                candidate = candidate.replace(month=now.month + 1)
        return candidate.isoformat()

    else:  # weekly (default)
        dca_day = env.get("DCA_DAY", "monday").lower()
        days    = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]
        target_weekday = days.index(dca_day) if dca_day in days else 0
        days_ahead = (target_weekday - now.weekday()) % 7
        if days_ahead == 0:
            scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if scheduled <= now:
                days_ahead = 7
        next_dt = (now + timedelta(days=days_ahead)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        return next_dt.isoformat()


def _format_trade(t):
    if not t:
        return None
    reason_labels = {
        "dca":                  "DCA Buy",
        "dip_buy_tier1":        "Dip Buy (Tier 1 — 7%+ drop)",
        "dip_buy_tier2":        "Dip Buy (Tier 2 — 15%+ drop)",
        "dip_buy_tier3":        "Dip Buy (Tier 3 — 22%+ crash)",
        "recycler_sell":        "Recycler Sell (taking profit)",
        "recycler_rebuy":       "Recycler Rebuy (buying back cheaper)",
        "usd_dca_sell":         "DCA Sell",
        "usd_spike_sell_tier1": "Spike Sell (Tier 1 — 7%+ rise)",
        "usd_spike_sell_tier2": "Spike Sell (Tier 2 — 15%+ rise)",
        "usd_spike_sell_tier3": "Spike Sell (Tier 3 — 22%+ pump)",
        "usd_recycler_buy":     "Recycler Buy (buying dip for resell)",
        "usd_recycler_resell":  "Recycler Resell (selling bounce)",
        "range_recycler_buy":   "Range Recycler Buy (sideways dip)",
        "range_recycler_sell":  "Range Recycler Sell (sideways pop)",
        # Strategy v2.0 reasons.
        "universal_recycler_open":  "Recycler Open (bought a slice)",
        "universal_recycler_close": "Recycler Close (sold a slice for profit)",
        "harvest_fire":             "Harvest (took profit on confirmed rally)",
        "onboarding":           "Onboarding (existing position)",
        "quick_buy":            "Quick Buy BTC (manual)",
    }
    return {
        "id":          t["id"],
        "timestamp":   t["timestamp"],
        "side":        t["side"],
        "reason":      t["reason"],
        "label":       reason_labels.get(t["reason"], t["reason"]),
        "btc_amount":  round(t["btc_amount"], 8),
        "usd_amount":  round(t["usd_amount"], 2),
        "price_usd":   round(t["price_usd"], 2),
        "fee_usd":     round(t["fee_usd"], 4),
        "active_mode": t.get("active_mode", "btc_accumulate"),
        "paper_trade": bool(t.get("paper_trade", 0)),
        # Maker-only fields (v1.5.0). Use .get so this stays safe against an
        # older DB that predates the migration; null until the reconciler fills.
        "ordertype":        t.get("ordertype"),
        "was_maker":        bool(t["was_maker"]) if t.get("was_maker") is not None else None,
        "fee_actual_usd":   round(t["fee_actual"], 4) if t.get("fee_actual") is not None else None,
        "price_actual_usd": round(t["price_actual"], 2) if t.get("price_actual") is not None else None,
        "fill_status":      t.get("fill_status"),
    }


def _calculate_mood(mode, pnl_pct, current_price, ma200,
                    waiting_rebuy, waiting_resell, trade_count):
    if trade_count == 0:
        return {"icon": "⏳", "label": "Warming Up", "color": "neutral",
                "detail": "Bot is running, waiting for first trade opportunity."}
    if waiting_rebuy:
        return {"icon": "🎯", "label": "Ready to Rebuy", "color": "positive",
                "detail": "Sold some BTC at a profit. Waiting for price to dip to buy back more."}
    if waiting_resell:
        return {"icon": "🎯", "label": "Ready to Resell", "color": "positive",
                "detail": "Bought BTC at a discount. Waiting for price to bounce to sell for more USD."}
    if mode == "usd_accumulate":
        if ma200 and current_price < ma200 * 0.85:
            return {"icon": "🛡️", "label": "Bear Market Shield", "color": "bear",
                    "detail": "Bear market detected. Selling into weakness, protecting your USD value."}
        return {"icon": "📉", "label": "USD Mode Active", "color": "bear",
                "detail": "Price trending below 200MA. Bot is accumulating USD, selling BTC strategically."}
    if ma200 and current_price > ma200 * 1.2:
        return {"icon": "🚀", "label": "Bull Run", "color": "bull",
                "detail": f"Price is {((current_price/ma200)-1)*100:.0f}% above 200MA. Stacking sats aggressively."}
    if pnl_pct > 0:
        return {"icon": "📈", "label": "In the Green", "color": "positive",
                "detail": f"Up {pnl_pct:.1f}% on your cost basis. Bot is stacking and watching for recycler opportunities."}
    if pnl_pct > -20:
        return {"icon": "🔄", "label": "DCA Working", "color": "neutral",
                "detail": f"Down {abs(pnl_pct):.1f}% on basis. Every weekly buy is pulling your average down. Patience."}
    return {"icon": "💎", "label": "Hold & Stack", "color": "neutral",
            "detail": f"Down {abs(pnl_pct):.1f}% on basis. This is where DCA earns its keep. Keep stacking."}


def _get_sideways_status():
    """Build sideways status dict for the API response."""
    env = _read_env()
    enabled = env.get("SIDEWAYS_ENABLED", "true").lower() in ("true", "1", "yes")
    is_active = _state("sideways_active", "false") == "true"
    range_pct = _state("sideways_range_pct")
    max_positions = int(env.get("RANGE_MAX_POSITIONS", "5"))

    # Count open range positions
    open_positions = 0
    conn = get_db()
    if conn:
        try:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM range_positions WHERE status='open'"
            ).fetchone()
            open_positions = row["c"] if row else 0
        except Exception:
            pass
        finally:
            conn.close()

    return {
        "active":             is_active and enabled,
        "enabled":            enabled,
        "range_pct":          round(float(range_pct), 2) if range_pct else None,
        "window_days":        14,
        "threshold_pct":      12,
        "positions":          open_positions,
        "max_positions":      max_positions,
        "buy_threshold_pct":  -4,
        "sell_threshold_pct": 6,
    }


def _get_throttle_status():
    """Build the anti-thrash throttle status block for /api/status.bot."""
    env = _read_env()
    min_gap     = int(env.get("MIN_GAP_BETWEEN_TRADES_SECONDS", "3600"))
    max_per_day = int(env.get("MAX_TRADES_PER_DAY", "8"))
    last_ts     = int(_state("last_trade_ts", "0") or 0)

    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state_date = _state("trades_today_date", "") or ""
    trades_today = int(_state("trades_today_count", "0") or 0) if state_date == today_utc else 0

    elapsed = int(time.time()) - last_ts
    seconds_until = max(0, min_gap - elapsed) if last_ts else 0

    return {
        "min_gap_seconds":            min_gap,
        "max_per_day":                max_per_day,
        "trades_today":               trades_today,
        "last_trade_ts":              last_ts or None,
        "seconds_until_next_allowed": seconds_until,
    }


def _get_volatility_status():
    """Build the volatility status block for /api/status.bot.

    Reads the metrics the bot loop persists to bot_state each tick; falls back to
    a neutral (multiplier 1.0) reading if the bot hasn't written them yet.
    """
    env = _read_env()
    enabled = env.get("VOLATILITY_ADAPTIVE_ENABLED", "true").lower() in ("true", "1", "yes")

    def _f(key):
        raw = _state(key, "")
        try:
            return float(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            return None

    atr_pct      = _f("atr_pct")
    baseline_pct = _f("atr_baseline_pct")
    multiplier   = _f("vol_multiplier")
    if multiplier is None:
        multiplier = 1.0

    if multiplier < 0.95:
        regime = "calm"
    elif multiplier > 1.10:
        regime = "storm"
    else:
        regime = "normal"

    return {
        "enabled":      enabled,
        "atr_pct":      round(atr_pct, 4) if atr_pct is not None else None,
        "baseline_pct": round(baseline_pct, 4) if baseline_pct is not None else None,
        "multiplier":   round(multiplier, 3),
        "regime":       regime,
    }


def _get_strategy_version() -> str:
    """Return the configured strategy engine ("v1" or "v2").

    Read from .env so the dashboard can degrade gracefully against a v1.x bot
    that has no v2 keys at all (defaults to "v1").
    """
    env = _read_env()
    return env.get("STRATEGY_VERSION", "v1").lower().strip()


def _get_regime_status() -> dict:
    """Build the v2 event-driven regime block from bot_state (additive, v2-only).

    Reads the keys regime_detector.py persists; returns a chop/cold-start default
    when those keys are absent (i.e. the bot is still on v1.x).
    """
    return {
        "state":      _state("regime_current", "chop"),
        "entered_at": _state("regime_entered_ts"),
    }


def _get_harvest_status() -> dict:
    """Build the v2 Harvest block from bot_state (additive, v2-only).

    Mirrors harvest.get_harvest_status using only bot_state reads so the API has
    no hard dependency on the bot package. All fields fall back to inert
    defaults when the bot is on v1.x and has never written them.
    """
    def _f(key, default=0.0):
        raw = _state(key, "")
        try:
            return float(raw) if raw not in (None, "") else default
        except (TypeError, ValueError):
            return default

    env = _read_env()
    total_cap_pct = float(env.get("HARVEST_TOTAL_CAP_PCT", "0.33"))
    start_stack   = _f("harvest_rally_start_stack")
    sold          = _f("harvest_rally_sold_btc")
    rally_cap_btc = start_stack * total_cap_pct
    return {
        "active":            _state("harvest_rally_active", "false") == "true",
        "rally_sold_btc":    round(sold, 8),
        "rally_cap_btc":     round(rally_cap_btc, 8),
        "cap_remaining_btc": round(max(0.0, rally_cap_btc - sold), 8),
        "threshold_pct":     float(env.get("HARVEST_THRESHOLD_PCT", "1.15")),
    }


def _get_operating_regime_status(current_price, ma200) -> dict:
    """Classify the v2 operating regime (Accumulate / Neutral / Harvest) for the
    dashboard, mirroring mode_manager.get_operating_regime without importing the
    bot package.

    Pure function of price vs the 200MA and the configured harvest bands. Falls
    back to "accumulate" with ma_available=False when the 200MA is not yet built
    (insufficient history) or the bot is on v1.x — the conservative,
    prime-directive-aligned default of "keep stacking until a rally is proven".
    """
    env = _read_env()
    try:
        threshold = float(env.get("HARVEST_THRESHOLD_PCT", "1.15"))
        exit_pct  = float(env.get("HARVEST_EXIT_PCT", "1.05"))
    except (TypeError, ValueError):
        threshold, exit_pct = 1.15, 1.05

    if not ma200 or ma200 <= 0 or not current_price:
        return {"operating_regime": "accumulate", "ma_ratio": None,
                "ma_available": False}

    ratio = current_price / ma200
    if ratio >= threshold:
        regime = "harvest"
    elif ratio >= exit_pct:
        regime = "neutral"
    else:
        regime = "accumulate"
    return {"operating_regime": regime, "ma_ratio": round(ratio, 4),
            "ma_available": True}


def _get_recycler_positions() -> list:
    """Return open v2 Universal Recycler positions for /api/status (additive).

    Empty list when the table has no open rows (including on a v1.x bot).
    """
    rows = query(
        "SELECT id, buy_price, btc_amount, sell_band_price, timestamp "
        "FROM range_positions WHERE status='open' ORDER BY timestamp ASC"
    )
    out = []
    for r in rows:
        out.append({
            "id":              r["id"],
            "buy_price":       round(r["buy_price"], 2) if r["buy_price"] else None,
            "btc_amount":      round(r["btc_amount"], 8) if r["btc_amount"] else None,
            "sell_band_price": round(r["sell_band_price"], 2)
                               if ("sell_band_price" in r.keys() and r["sell_band_price"]) else None,
            "opened_at":       r["timestamp"],
        })
    return out


def _btc_stack_history():
    rows = query("""
        SELECT DATE(timestamp) as date, btc_balance, avg_cost_basis
        FROM portfolio_snapshots ORDER BY timestamp ASC
    """)
    seen = {}
    for r in rows:
        seen[r["date"]] = r
    return [{"date": d, "btc": round(v["btc_balance"], 8), "basis": round(v["avg_cost_basis"], 2)}
            for d, v in sorted(seen.items())]


# ── Setup wizard page ──────────────────────────────────────────────────

@app.route("/about")
def about_page():
    return send_from_directory(app.static_folder, "about.html")

@app.route("/setup-guide")
def setup_guide_page():
    return send_from_directory(app.static_folder, "setup-guide.html")

@app.route("/")
@app.route("/setup")
def setup_page():
    """Serve the setup wizard. After setup, users connect via BotCoin-Dash."""
    configured = ENV_PATH.exists() and bool(_read_env().get("KRAKEN_API_KEY", "").strip())
    if configured:
        return jsonify({"status": "configured", "message": "Bot is configured. Connect via BotCoin-Dash."}), 200
    return send_from_directory(app.static_folder, "setup.html")


# ── Health (public — no auth) ─────────────────────────────────────────────────

@app.route("/api/health")
def health():
    """Public endpoint — lets dashboard check if bot server is reachable."""
    configured = ENV_PATH.exists() and bool(_read_env().get("KRAKEN_API_KEY", "").strip())
    return jsonify({
        "status":     "ok",
        "configured": configured,
        "version":    _read_local_version(),
    })


# ── Status (auth required) ────────────────────────────────────────────────────

@app.route("/api/status")
@requires_auth
def status():
    snapshot       = query_one("SELECT * FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT 1")
    active_mode    = _state("active_mode", "btc_accumulate")
    onboarding_btc = float(_state("onboarding_btc_balance", "0") or 0)
    waiting_rebuy  = _state("btc_recycler_waiting_rebuy", "false") == "true"
    waiting_resell = _state("usd_recycler_waiting_resell", "false") == "true"
    last_switch_ts = _state("mode_last_switch_timestamp")
    ma200          = _get_200ma()

    trades = query("SELECT * FROM trades ORDER BY timestamp ASC")
    buys   = [t for t in trades if t["side"] == "buy"  and t["reason"] != "onboarding"]
    sells  = [t for t in trades if t["side"] == "sell"]

    total_btc_bought = sum(t["btc_amount"] for t in buys)
    total_btc_sold   = sum(t["btc_amount"] for t in sells)
    total_fees       = sum(t["fee_usd"] for t in trades if t["reason"] != "onboarding")
    bot_net_btc      = total_btc_bought - total_btc_sold

    last_trade    = query_one("SELECT * FROM trades WHERE reason != 'onboarding' ORDER BY timestamp DESC LIMIT 1")
    price_history = query("SELECT date, price_usd FROM daily_prices ORDER BY date ASC")

    # Snapshot values as fallback
    btc_balance = snapshot["btc_balance"]    if snapshot else onboarding_btc
    usd_balance = snapshot["usd_balance"]    if snapshot else 0

    # ── Average cost basis ────────────────────────────────────────────────────
    # "YOUR AVG COST" is the lay-person question "what did I pay per BTC on
    # average?" — NOT a tax-style cost basis of the remaining position.
    #
    # We therefore compute a simple volume-weighted average across EVERY buy the
    # user has ever made (the onboarding pseudo-trade for pre-existing BTC, plus
    # all DCA/dip buys and recycler rebuys) and deliberately IGNORE sells.
    #
    # Why ignore sells: the USD Recycler intentionally cycles capital — it sells
    # BTC and rebuys lower to bank "house money". A remaining-position basis like
    #   (total_invested - total_received_from_sells) / current_btc
    # collapses toward zero (or negative) once sells recoup the original outlay,
    # and dividing by the small leftover stack produced the absurd ~$17B/BTC
    # figure this field used to report. Averaging buys avoids that singularity
    # entirely and matches the "N days @ $price" stack-header stat the dashboard
    # already shows. The separate P&L stat covers realised gains.
    all_buys      = [t for t in trades if t["side"] == "buy"]
    buys_btc      = sum(t["btc_amount"] for t in all_buys)
    buys_cost_usd = sum(t["usd_amount"] + t["fee_usd"] for t in all_buys)
    if buys_btc > 0:
        avg_basis = buys_cost_usd / buys_btc
    else:
        # No buys recorded yet — fall back to last snapshot (or 0 on a cold start).
        avg_basis = snapshot["avg_cost_basis"] if snapshot else 0

    # Live Kraken balances (override snapshot)
    live = _get_live_balances()
    if live:
        usd_balance = live["usd"]
        if live["btc"] > 0:
            btc_balance = live["btc"]

    # Live price from Kraken ticker (sync with bot's 5-min loop)
    # Fall back to last DB sample if the ticker call fails
    live_price = _get_live_price()
    latest_price_row = query_one("SELECT price_usd FROM price_history ORDER BY timestamp DESC LIMIT 1")
    current_price = live_price if live_price else (latest_price_row["price_usd"] if latest_price_row else 0)

    # Recent high over last 7 days — used by dip-buy logic as reference
    recent_high_row = query_one(
        "SELECT MAX(price_usd) as high FROM price_history "
        "WHERE timestamp >= datetime('now', '-7 days')"
    )
    recent_high = recent_high_row["high"] if recent_high_row and recent_high_row["high"] else current_price

    portfolio_value = btc_balance * current_price if current_price else 0
    pnl_pct = ((current_price - avg_basis) / avg_basis * 100) if avg_basis and current_price else 0

    mood = _calculate_mood(active_mode, pnl_pct, current_price, ma200,
                           waiting_rebuy, waiting_resell, len(buys))

    return jsonify({
        "portfolio": {
            "btc_balance":     round(btc_balance, 8),
            "usd_balance":     round(usd_balance, 2),
            "portfolio_value": round(portfolio_value, 2),
            "avg_cost_basis":  round(avg_basis, 2),
            "current_price":   round(current_price, 2),
            "pnl_pct":         round(pnl_pct, 2),
            "breakeven_price": round(avg_basis, 2),
        },
        "bot": {
            "active_mode":     active_mode,
            "bot_net_btc":     round(bot_net_btc, 8),
            "total_fees_paid": round(total_fees, 4),
            "trade_count":     len([t for t in trades if t["reason"] != "onboarding"]),
            "ma200":           round(ma200, 2) if ma200 else None,
            "ma_days_built":   len(price_history),
            "waiting_rebuy":   waiting_rebuy,
            "waiting_resell":  waiting_resell,
            "last_switch":     last_switch_ts,
            "recent_high":     round(recent_high, 2) if recent_high else None,
            "trend":           _trend_duration(ma200),
            "sideways":        _get_sideways_status(),
            "throttle":        _get_throttle_status(),
            "volatility":      _get_volatility_status(),
            # ── Strategy v2.0 additive fields (inert defaults on a v1.x bot). ──
            "strategy_version":   _get_strategy_version(),
            "regime_state":       _get_regime_status()["state"],
            "regime_substate":    _get_regime_status()["entered_at"],
            "operating_regime":   _get_operating_regime_status(current_price, ma200),
            "harvest_state":      _get_harvest_status(),
            "recycler_positions": _get_recycler_positions(),
        },
        "version":    _read_local_version(),
        "mood":       mood,
        "last_trade": _format_trade(last_trade),
        "next_dca":   _next_dca_timestamp(),
        "chart":      _btc_stack_history(),
        "price_history": [{"date": r["date"], "price": r["price_usd"]} for r in price_history[-90:]],
    })


# ── Trades (auth required) ────────────────────────────────────────────────────

@app.route("/api/trades")
@requires_auth
def trades():
    rows = query("SELECT * FROM trades WHERE reason != 'onboarding' ORDER BY timestamp DESC LIMIT 500")
    return jsonify([_format_trade(t) for t in rows])


@app.route("/api/deposits")
@requires_auth
def deposits():
    """
    Return historical deposits with at-time USD valuation.

    On each hit we lazily sync new Kraken deposit-ledger entries into the local
    `deposits` table (idempotent via kraken_refid), looking up the BTC/USD price
    on each BTC deposit's day from CoinGecko (cached, with a courtesy sleep
    between uncached lookups to respect the free-tier rate limit). USD deposits
    are valued at face. We then return every stored deposit plus rolled-up
    totals. No background polling — sync happens here, lazily.
    """
    new_deposits = _fetch_kraken_deposits()

    if new_deposits:
        # Which refids do we already have? Avoid re-pricing known deposits.
        existing = {r["kraken_refid"] for r in
                    query("SELECT kraken_refid FROM deposits")}
        conn = get_db(readonly=False)
        if conn:
            try:
                _ensure_deposit_tables(conn)
                uncached_lookups = 0
                for dep in new_deposits:
                    if dep["refid"] in existing:
                        continue
                    if dep["currency"] == "BTC":
                        date_key = dep["timestamp"][:10]
                        had_cache = query_one(
                            "SELECT 1 FROM btc_price_history WHERE date=?", (date_key,)
                        )
                        # Pace only genuine CoinGecko calls (free tier ~30/min).
                        if not had_cache and uncached_lookups > 0:
                            time.sleep(2)
                        price = _coingecko_price_at(date_key)
                        if not had_cache:
                            uncached_lookups += 1
                        usd_value = (dep["amount"] * price) if price else 0.0
                        _record_deposit(conn, dep["refid"], "BTC", dep["amount"],
                                        dep["timestamp"], price, usd_value)
                    else:  # USD
                        _record_deposit(conn, dep["refid"], "USD", dep["amount"],
                                        dep["timestamp"], None, dep["amount"])
                conn.commit()
            except Exception as e:
                import logging as _l
                _l.getLogger("botapi").warning("Deposit sync failed: %s", e)
            finally:
                conn.close()

    rows = query("SELECT * FROM deposits ORDER BY timestamp ASC")
    out = [{
        "currency":          r["currency"],
        "amount":            round(r["amount"], 8),
        "timestamp":         r["timestamp"],
        "price_usd_at_time": round(r["price_usd_at_time"], 2) if r["price_usd_at_time"] is not None else None,
        "usd_value_at_time": round(r["usd_value_at_time"], 2),
    } for r in rows]

    btc_rows = [r for r in out if r["currency"] == "BTC"]
    usd_rows = [r for r in out if r["currency"] == "USD"]
    totals = {
        "btc_deposits_count":            len(btc_rows),
        "btc_deposits_amount":           round(sum(r["amount"] for r in btc_rows), 8),
        "btc_deposits_usd_value_at_time": round(sum(r["usd_value_at_time"] for r in btc_rows), 2),
        "usd_deposits_amount":           round(sum(r["amount"] for r in usd_rows), 2),
    }
    return jsonify({"deposits": out, "totals": totals})


@app.route("/api/open_orders")
@requires_auth
def open_orders():
    """Fetch open (unfilled) limit orders from Kraken."""
    try:
        env        = _read_env()
        api_key    = env.get("KRAKEN_API_KEY", "").strip()
        api_secret = env.get("KRAKEN_API_SECRET", "").strip()
        if not api_key or not api_secret:
            return jsonify({"open_orders": []})

        urlpath = "/0/private/OpenOrders"
        nonce   = str(int(time.time() * 1000))
        data    = {"nonce": nonce}
        post    = urllib.parse.urlencode(data).encode()
        sig     = _kraken_sign(urlpath, data, api_secret)

        req = urllib.request.Request(
            "https://api.kraken.com" + urlpath,
            data=post,
            headers={
                "API-Key":  api_key,
                "API-Sign": sig,
                "Content-Type": "application/x-www-form-urlencoded",
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())

        if result.get("error"):
            return jsonify({"open_orders": []})

        orders = result.get("result", {}).get("open", {})
        parsed = []
        for txid, o in orders.items():
            desc = o.get("descr", {})
            parsed.append({
                "txid":   txid,
                "type":   desc.get("type", ""),       # buy / sell
                "price":  desc.get("price", ""),       # limit price
                "volume": o.get("vol", ""),            # BTC volume
                "filled": o.get("vol_exec", "0"),      # filled so far
                "status": o.get("status", ""),
                "pair":   desc.get("pair", ""),
            })
        return jsonify({"open_orders": parsed})
    except Exception as e:
        return jsonify({"open_orders": [], "error": str(e)})


# ── Settings GET (auth required) ──────────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
@requires_auth
def get_settings():
    env = _read_env()

    # Volatility metrics + effective (vol-adjusted) dip thresholds.
    vol = _get_volatility_status()
    multiplier = vol["multiplier"]
    base_t1 = float(env.get("DIP_THRESHOLD_PERCENT", "0.07"))
    base_t2 = float(env.get("DIP_TIER2_THRESHOLD_PERCENT", "0.15"))
    base_t3 = float(env.get("DIP_TIER3_THRESHOLD_PERCENT", "0.22"))

    return jsonify({
        "dca_amount":        env.get("DCA_AMOUNT_USD", "50.00"),
        "dca_frequency":     env.get("DCA_FREQUENCY", "weekly"),
        "dca_day":           env.get("DCA_DAY", "monday"),
        "dca_day_of_month":  env.get("DCA_DAY_OF_MONTH", "1"),
        "dca_time_utc":      env.get("DCA_TIME_UTC", "13:00"),
        "mode":              env.get("MODE", "auto"),
        "dip_tier1":         env.get("DIP_THRESHOLD_PERCENT", "0.07"),
        "dip_tier2":         env.get("DIP_TIER2_THRESHOLD_PERCENT", "0.15"),
        "dip_tier3":         env.get("DIP_TIER3_THRESHOLD_PERCENT", "0.22"),
        "recycler_sell_threshold": env.get("RECYCLER_SELL_THRESHOLD_PERCENT", "0.18"),
        "recycler_pool_percent":   env.get("RECYCLER_POOL_PERCENT", "0.35"),
        "max_order_usd":     env.get("MAX_ORDER_USD", "1000.00"),
        "sideways_enabled":      env.get("SIDEWAYS_ENABLED", "true"),
        "range_trade_size_usd":  env.get("RANGE_TRADE_SIZE_USD", "500"),
        "range_max_positions":   env.get("RANGE_MAX_POSITIONS", "5"),
        # Volatility-adaptive thresholds (v1.5.0)
        "volatility_adaptive_enabled": env.get("VOLATILITY_ADAPTIVE_ENABLED", "true"),
        "atr_pct":            vol["atr_pct"],
        "atr_baseline_pct":   vol["baseline_pct"],
        "vol_multiplier":     multiplier,
        "effective_dip_tier1": round(base_t1 * multiplier, 4),
        "effective_dip_tier2": round(base_t2 * multiplier, 4),
        "effective_dip_tier3": round(base_t3 * multiplier, 4),
        # Anti-thrash dampener (v1.5.0)
        "min_gap_between_trades_seconds": env.get("MIN_GAP_BETWEEN_TRADES_SECONDS", "3600"),
        "max_trades_per_day":             env.get("MAX_TRADES_PER_DAY", "8"),
    })


# ── Settings POST (auth required) ─────────────────────────────────────────────

@app.route("/api/settings", methods=["POST"])
@requires_auth
def save_settings():
    body = request.get_json(force=True)
    # Validate frequency if provided
    freq = body.get("dca_frequency")
    if freq and freq not in ("daily", "weekly", "monthly"):
        return jsonify({"ok": False, "error": "dca_frequency must be daily, weekly, or monthly"}), 400

    allowed = {
        "DCA_AMOUNT_USD":                  body.get("dca_amount"),
        "DCA_FREQUENCY":                   body.get("dca_frequency"),
        "DCA_DAY":                         body.get("dca_day"),
        "DCA_DAY_OF_MONTH":                body.get("dca_day_of_month"),
        "DCA_TIME_UTC":                    body.get("dca_time_utc"),
        "MODE":                            body.get("mode"),
        "DIP_THRESHOLD_PERCENT":           body.get("dip_tier1"),
        "DIP_TIER2_THRESHOLD_PERCENT":     body.get("dip_tier2"),
        "DIP_TIER3_THRESHOLD_PERCENT":     body.get("dip_tier3"),
        "RECYCLER_SELL_THRESHOLD_PERCENT": body.get("recycler_sell_threshold"),
        "RECYCLER_POOL_PERCENT":           body.get("recycler_pool_percent"),
        "MAX_ORDER_USD":                   body.get("max_order_usd"),
        "SIDEWAYS_ENABLED":                body.get("sideways_enabled"),
        "RANGE_TRADE_SIZE_USD":            body.get("range_trade_size_usd"),
        "RANGE_MAX_POSITIONS":             body.get("range_max_positions"),
        # Anti-thrash dampener (v1.5.0)
        "MIN_GAP_BETWEEN_TRADES_SECONDS":  body.get("min_gap_between_trades_seconds"),
        "MAX_TRADES_PER_DAY":              body.get("max_trades_per_day"),
        # ── Strategy v2.0 keys (dormant on v1; active when STRATEGY_VERSION=v2) ──
        # These come from two places in the dashboard: (1) the aggression dial,
        # which writes the five size knobs as a preset bundle; (2) the expert
        # drawer, which lets the operator tune any of the 22 v2 keys directly.
        # All keys are accepted on a v1.5.2 bot too — they simply sit in .env
        # unused until the operator flips STRATEGY_VERSION to v2.
        "STRATEGY_VERSION":                body.get("strategy_version"),
        "HARVEST_THRESHOLD_PCT":           body.get("harvest_threshold_pct"),
        "HARVEST_EXIT_PCT":                body.get("harvest_exit_pct"),
        "HARVEST_SUSTAIN_DAYS":            body.get("harvest_sustain_days"),
        "HARVEST_FIRE_CAP_PCT":            body.get("harvest_fire_cap_pct"),
        "HARVEST_TOTAL_CAP_PCT":           body.get("harvest_total_cap_pct"),
        "HARVEST_TIER_TRIGGER_PCT":        body.get("harvest_tier_trigger_pct"),
        "HARVEST_MIN_STACK_BTC":           body.get("harvest_min_stack_btc"),
        "RECYCLER_POSITION_CAP_USD":       body.get("recycler_position_cap_usd"),
        "RECYCLER_POSITION_PCT":           body.get("recycler_position_pct"),
        "RECYCLER_MIN_POSITIONS":          body.get("recycler_min_positions"),
        "RECYCLER_POSITION_DIVISOR":       body.get("recycler_position_divisor"),
        "RECYCLER_TIME_LIMIT_DAYS":        body.get("recycler_time_limit_days"),
        "RECYCLER_BAND_REFERENCE":         body.get("recycler_band_reference"),
        "BREAKOUT_ATR_MULTIPLIER":         body.get("breakout_atr_multiplier"),
        "BREAKDOWN_ATR_MULTIPLIER":        body.get("breakdown_atr_multiplier"),
        "REGIME_BREAK_LOOKBACK_DAYS":      body.get("regime_break_lookback_days"),
        "REGIME_COOLING_QUIET_HOURS":      body.get("regime_cooling_quiet_hours"),
        "BREAKDOWN_MAX_HOLD_DAYS":         body.get("breakdown_max_hold_days"),
        "DCA_HARVEST_SCALE":               body.get("dca_harvest_scale"),
        "REBUILD_DCA_AMOUNT_USD":          body.get("rebuild_dca_amount_usd"),
        "REBUILD_DCA_DAYS":                body.get("rebuild_dca_days"),
    }
    # STRATEGY_VERSION: validate before accepting (only "v1" or "v2").
    if allowed.get("STRATEGY_VERSION") is not None:
        sv = str(allowed["STRATEGY_VERSION"]).lower().strip()
        if sv not in ("v1", "v2"):
            return jsonify({"ok": False, "error": "strategy_version must be 'v1' or 'v2'"}), 400
        allowed["STRATEGY_VERSION"] = sv
    # RECYCLER_BAND_REFERENCE: validate before accepting.
    if allowed.get("RECYCLER_BAND_REFERENCE") is not None:
        rbr = str(allowed["RECYCLER_BAND_REFERENCE"]).lower().strip()
        if rbr not in ("vwap_24h", "mid_recent_hl", "last_close"):
            return jsonify({"ok": False,
                            "error": "recycler_band_reference must be one of vwap_24h, mid_recent_hl, last_close"}), 400
        allowed["RECYCLER_BAND_REFERENCE"] = rbr
    # Volatility-adaptive toggle is boolean-only: normalise truthy/falsy input.
    if body.get("volatility_adaptive_enabled") is not None:
        v = body.get("volatility_adaptive_enabled")
        truthy = str(v).strip().lower() in ("true", "1", "yes", "on") if not isinstance(v, bool) else v
        allowed["VOLATILITY_ADAPTIVE_ENABLED"] = "true" if truthy else "false"
    updates = {k: str(v) for k, v in allowed.items() if v is not None}
    try:
        _write_env(updates)
        return jsonify({"ok": True, "message": "Settings saved. Bot picks up changes on next cycle (within 5 min)."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Setup: validate keys (no auth — called before password exists) ─────────────

@app.route("/api/setup/validate-keys", methods=["POST"])
def setup_validate_keys():
    # Block if already configured — prevents credential overwrite attacks
    if ENV_PATH.exists() and bool(_read_env().get("KRAKEN_API_KEY", "").strip()):
        return jsonify({"ok": False, "error": "Bot already configured."}), 403

    # Rate limit by IP even without auth
    ip = request.remote_addr
    if not _check_rate_limit(ip):
        return jsonify({"ok": False, "error": "Too many attempts. Try again in 60 seconds."}), 429

    body       = request.get_json(force=True)
    api_key    = body.get("api_key", "").strip()
    api_secret = body.get("api_secret", "").strip()
    if not api_key or not api_secret:
        return jsonify({"ok": False, "error": "API key and secret are required."}), 400

    try:
        urlpath = "/0/private/Balance"
        nonce   = str(int(time.time() * 1000))
        data    = {"nonce": nonce}
        post    = urllib.parse.urlencode(data).encode()
        sig     = _kraken_sign(urlpath, data, api_secret)
        req     = urllib.request.Request(
            "https://api.kraken.com" + urlpath,
            data=post,
            headers={
                "API-Key":  api_key,
                "API-Sign": sig,
                "Content-Type": "application/x-www-form-urlencoded",
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body_resp = json.loads(resp.read())

        if body_resp.get("error"):
            _record_failure(ip)
            return jsonify({"ok": False, "error": ", ".join(body_resp["error"])}), 400

        balances = body_resp.get("result", {})
        btc = float(balances.get("XXBT", balances.get("XBT", 0)))
        usd = float(balances.get("ZUSD", balances.get("USD", 0)))
        _record_success(ip)
        return jsonify({"ok": True, "balances": {"btc": round(btc, 8), "usd": round(usd, 2)}})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Setup: complete wizard (no auth — called before password exists) ───────────

@app.route("/api/setup/complete", methods=["POST"])
def setup_complete():
    # Block if already configured — prevents credential overwrite attacks
    if ENV_PATH.exists() and bool(_read_env().get("KRAKEN_API_KEY", "").strip()):
        return jsonify({"ok": False, "error": "Bot already configured. Use /api/settings to update."}), 403

    body = request.get_json(force=True)
    required = ["api_key", "api_secret", "dashboard_password", "dca_amount", "dca_day", "mode"]
    for field in required:
        if not body.get(field):
            return jsonify({"ok": False, "error": f"Missing field: {field}"}), 400

    updates = {
        "KRAKEN_API_KEY":                  body["api_key"].strip(),
        "KRAKEN_API_SECRET":               body["api_secret"].strip(),
        "DASHBOARD_PASSWORD":              body["dashboard_password"].strip(),
        "TRADING_PAIR":                    "XBTUSD",
        "MODE":                            body["mode"].strip(),
        "DCA_AMOUNT_USD":                  str(float(body["dca_amount"])),
        "DCA_DAY":                         body["dca_day"].strip().lower(),
        "DCA_TIME_UTC":                    body.get("dca_time_utc", "13:00").strip(),
        "RECYCLER_POOL_PERCENT":           "0.35",
        "DIP_THRESHOLD_PERCENT":           "0.07",
        "DIP_TIER2_THRESHOLD_PERCENT":     "0.15",
        "DIP_TIER3_THRESHOLD_PERCENT":     "0.22",
        "DIP_COOLDOWN_HOURS":              "12",
        "RECYCLER_SELL_THRESHOLD_PERCENT": "0.18",
        "RECYCLER_SELL_PERCENT":           "0.18",
        "RECYCLER_SELL_COOLDOWN_HOURS":    "36",
        "RECYCLER_REBUY_DROP_PERCENT":     "0.08",
        "KRAKEN_MAKER_FEE":                "0.0025",
        "MAX_ORDER_USD":                   "2000.00",
        "LOG_LEVEL":                       "INFO",
        "LOG_FILE":                        "/app/data/bot.log",
    }
    try:
        _write_env(updates)
        # Reload into current process so bot API picks up immediately
        for k, v in updates.items():
            os.environ[k] = v
        return jsonify({"ok": True, "message": "Configuration saved. Bot will start automatically."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Version check (public — no auth) ─────────────────────────────────────────────

GITHUB_RAW = "https://raw.githubusercontent.com/helprose98/botcoin-bot/main/VERSION"
VERSION_PATH = Path("/app/VERSION")


def _read_local_version() -> str:
    try:
        return VERSION_PATH.read_text().strip()
    except Exception:
        return "unknown"


def _fetch_latest_version() -> str | None:
    try:
        req = urllib.request.Request(GITHUB_RAW, headers={"User-Agent": "BotCoin/2.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read().decode().strip()
    except Exception:
        return None


def _version_gt(a: str, b: str) -> bool:
    """Return True if version a is greater than version b.

    Handles SemVer pre-release suffixes (e.g. '2.0.0-dev', '2.0.0-rc.1')
    by stripping anything after a hyphen before parsing. This matches
    PEP 440's release-segment-first ordering for our update-check use case;
    we never need to distinguish between 2.0.0-dev and 2.0.0 for 'is there a
    new release?' — a new MAJOR.MINOR.PATCH wins regardless of suffix.
    """
    def _parse(v: str) -> tuple:
        base = v.split("-", 1)[0]  # strip pre-release suffix
        return tuple(int(x) for x in base.split("."))
    try:
        return _parse(a) > _parse(b)
    except Exception:
        return False


@app.route("/api/version")
def version():
    """Public endpoint — returns current version and checks GitHub for updates."""
    current = _read_local_version()
    latest  = _fetch_latest_version()
    update_available = latest is not None and _version_gt(latest, current)
    return jsonify({
        "current":          current,
        "latest":           latest or current,
        "update_available": update_available,
    })


# ── Maker stats (auth required) ───────────────────────────────────────────────

@app.route("/api/maker_stats")
@requires_auth
def maker_stats():
    """
    Maker-fill performance for the current calendar month: how many real
    (non-paper) orders rested as maker, the fees actually paid, and an estimate
    of fees saved versus a taker baseline for the same notional.
    """
    env = _read_env()
    taker_baseline = float(env.get("KRAKEN_TAKER_FEE", "0.0026"))

    month_start = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    ).isoformat()

    rows = query(
        """
        SELECT was_maker, fee_usd, fee_actual, usd_amount
        FROM trades
        WHERE paper_trade = 0 AND timestamp >= ?
        """,
        (month_start,),
    )

    total       = len(rows)
    maker_fills = 0
    fees_paid   = 0.0
    taker_cost  = 0.0
    for r in rows:
        # fee_actual is the reconciled value; fall back to the estimated fee_usd.
        fee = r.get("fee_actual")
        if fee in (None, ""):
            fee = r.get("fee_usd") or 0.0
        fees_paid += float(fee)

        notional = float(r.get("usd_amount") or 0.0)
        taker_cost += notional * taker_baseline

        if r.get("was_maker") == 1:
            maker_fills += 1

    fill_rate = (maker_fills / total) if total else 0.0
    fees_saved = taker_cost - fees_paid

    return jsonify({
        "month_start":        month_start,
        "total_orders":       total,
        "maker_fills":        maker_fills,
        "maker_fill_rate":    round(fill_rate, 4),
        "fees_paid_usd":      round(fees_paid, 4),
        "taker_baseline_fee": taker_baseline,
        "taker_cost_usd":     round(taker_cost, 4),
        "fees_saved_usd":     round(fees_saved, 4),
    })


# ── Update (auth required) ──────────────────────────────────────────────────────



# ── DCA Baseline (auth required) ──────────────────────────────────────────────

@app.route("/api/dca_baseline")
@requires_auth
def dca_baseline():
    """
    Compute a shadow 'pure DCA' portfolio alongside the real bot portfolio.
    Shadow portfolio: only executes the configured DCA schedule from the
    bot's first recorded trade date. No dip buys, no recycler, no quick buys.
    """
    env = _read_env()
    dca_amount  = float(env.get("DCA_AMOUNT_USD", "10.0"))
    dca_freq    = env.get("DCA_FREQUENCY", "daily").lower()
    current_price = _get_live_price()
    if not current_price:
        row = query_one("SELECT price_usd FROM price_history ORDER BY timestamp DESC LIMIT 1")
        current_price = row["price_usd"] if row else 0
    if not current_price:
        return jsonify({"ok": False, "error": "No price data available"}), 503

    first_trade = query_one(
        "SELECT timestamp FROM trades WHERE reason != 'onboarding' ORDER BY timestamp ASC LIMIT 1"
    )
    if not first_trade:
        return jsonify({"ok": False, "error": "No trades recorded yet"}), 404

    start_dt = datetime.fromisoformat(first_trade["timestamp"]).replace(tzinfo=timezone.utc)
    now_utc  = datetime.now(timezone.utc)

    # Build shadow DCA dates
    shadow_dates = []
    cursor = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if dca_freq == "daily":
        while cursor <= now_utc:
            shadow_dates.append(cursor)
            cursor += timedelta(days=1)
    elif dca_freq == "weekly":
        while cursor <= now_utc:
            shadow_dates.append(cursor)
            cursor += timedelta(weeks=1)
    elif dca_freq == "monthly":
        while cursor <= now_utc:
            shadow_dates.append(cursor)
            month = cursor.month + 1
            year  = cursor.year + (month - 1) // 12
            month = ((month - 1) % 12) + 1
            try:
                cursor = cursor.replace(year=year, month=month)
            except ValueError:
                import calendar
                last_day = calendar.monthrange(year, month)[1]
                cursor = cursor.replace(year=year, month=month, day=last_day)

    # Build price map from DB
    price_map = {}
    for r in query("SELECT DATE(date) as d, price_usd FROM daily_prices ORDER BY date ASC"):
        price_map[r["d"]] = r["price_usd"]
    for r in query("SELECT DATE(timestamp) as d, AVG(price_usd) as price_usd FROM price_history GROUP BY DATE(timestamp)"):
        if r["d"] not in price_map:
            price_map[r["d"]] = r["price_usd"]

    def nearest_price(dt):
        target = dt.strftime("%Y-%m-%d")
        if target in price_map:
            return price_map[target]
        for delta in range(1, 8):
            for sign in (-1, 1):
                key = (dt + timedelta(days=delta * sign)).strftime("%Y-%m-%d")
                if key in price_map:
                    return price_map[key]
        return current_price

    # Simulate shadow portfolio
    shadow_btc = 0.0
    shadow_spent = 0.0
    shadow_fees = 0.0
    maker_fee = 0.0016
    for date in shadow_dates:
        price = nearest_price(date)
        if price and price > 0:
            fee_usd    = dca_amount * maker_fee
            net_usd    = dca_amount - fee_usd
            shadow_btc   += net_usd / price
            shadow_spent += dca_amount
            shadow_fees  += fee_usd

    # Real bot stats
    all_trades = query("SELECT * FROM trades WHERE reason != 'onboarding' ORDER BY timestamp ASC")
    real_btc_bought = 0.0
    real_usd_spent  = 0.0
    real_fees       = 0.0
    recycler_btc    = 0.0
    recycler_usd_gained = 0.0
    for t in all_trades:
        btc    = float(t["btc_amount"] or 0)
        usd    = float(t["usd_amount"] or 0)
        fee    = float(t["fee_usd"]    or 0)
        reason = (t["reason"] or "").lower()
        side   = (t["side"]   or "").lower()
        real_fees += fee
        if side == "buy":
            real_btc_bought += btc
            if reason in ("recycler_rebuy", "usd_recycler_buy"):
                recycler_btc += btc
            else:
                real_usd_spent += usd
        elif side == "sell":
            if "recycl" in reason or "spike" in reason or reason == "usd_dca_sell":
                recycler_usd_gained += usd

    snapshot = query_one("SELECT btc_balance, usd_balance FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT 1")
    live_btc = float(snapshot["btc_balance"]) if snapshot else real_btc_bought

    shadow_value = round(shadow_btc * current_price, 2)
    real_value   = round(live_btc   * current_price, 2)
    delta_btc    = round(live_btc - shadow_btc, 8)
    delta_usd    = round(real_value - shadow_value, 2)

    return jsonify({
        "ok":            True,
        "start_date":    start_dt.strftime("%Y-%m-%d"),
        "dca_amount":    dca_amount,
        "dca_frequency": dca_freq,
        "days_running":  (now_utc - start_dt).days,
        "current_price": round(current_price, 2),
        "baseline": {
            "btc":       round(shadow_btc, 8),
            "usd_spent": round(shadow_spent, 2),
            "fees":      round(shadow_fees, 4),
            "value_usd": shadow_value,
            "dca_count": len(shadow_dates),
        },
        "actual": {
            "btc":       round(live_btc, 8),
            "usd_spent": round(real_usd_spent, 2),
            "fees":      round(real_fees, 4),
            "value_usd": real_value,
            "recycler_btc": round(recycler_btc, 8),
            "recycler_usd_gained": round(recycler_usd_gained, 2),
        },
        "delta": {
            "btc":           delta_btc,
            "usd":           delta_usd,
            "pct":           round((delta_usd / shadow_value * 100), 2) if shadow_value > 0 else 0,
            "outperforming": delta_usd >= 0,
        },
    })

@app.route("/api/update", methods=["POST"])
@requires_auth
def update():
    """
    Pull latest code from GitHub and rebuild containers.
    Runs /app/update.sh in the background — bot will restart automatically.
    """
    # Write a trigger file on the shared data volume.
    # A host-side cron job watches for this file and runs update.sh
    # This way the update runs on the HOST and survives container restarts.
    trigger = Path("/app/data/update.trigger")
    try:
        trigger.write_text("update")
        return jsonify({
            "ok":      True,
            "message": "Update started. Bot will restart in ~2 minutes. Refresh the dashboard."
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8081, debug=False)
