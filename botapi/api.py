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


def _record_quick_buy_trade(order_id: str, btc_amount: float, usd_amount: float,
                             price_usd: float, fee_usd: float) -> bool:
    """
    Strictly controlled write — only inserts a quick_buy trade record.
    This is the ONLY write operation botapi is allowed to perform.
    No arbitrary SQL. No updates. No deletes.
    """
    try:
        conn = get_db(readonly=False)
        if not conn:
            return False
        net_usd = -(usd_amount + fee_usd)
        conn.execute("""
            INSERT INTO trades
            (order_id, side, reason, btc_amount, usd_amount, price_usd, fee_usd, net_usd, active_mode, paper_trade)
            VALUES (?, 'buy', 'quick_buy', ?, ?, ?, ?, ?, 'btc_accumulate', 0)
        """, (order_id, round(btc_amount, 8), round(usd_amount, 2),
               round(price_usd, 2), round(fee_usd, 4), round(net_usd, 4)))
        conn.commit()
        return True
    except Exception as e:
        import logging as _l
        _l.getLogger("botapi").warning("Trade record failed: %s", e)
        return False
    finally:
        if conn:
            conn.close()


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


def _get_200ma():
    rows = query("SELECT price_usd FROM daily_prices ORDER BY date DESC LIMIT 200")
    if len(rows) < 10:
        return None
    return sum(r["price_usd"] for r in rows) / len(rows)


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
        "version":    "2.0",
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
    avg_basis   = snapshot["avg_cost_basis"] if snapshot else 0

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
        },
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
    return jsonify({
        "dca_amount":        env.get("DCA_AMOUNT_USD", "50.00"),
        "dca_frequency":     env.get("DCA_FREQUENCY", "weekly"),
        "dca_day":           env.get("DCA_DAY", "monday"),
        "dca_day_of_month":  env.get("DCA_DAY_OF_MONTH", "1"),
        "dca_time_utc":      env.get("DCA_TIME_UTC", "13:00"),
        "mode":              env.get("MODE", "auto"),
        "paper_trading":     env.get("PAPER_TRADING", "false"),
        "dip_tier1":         env.get("DIP_THRESHOLD_PERCENT", "0.07"),
        "dip_tier2":         env.get("DIP_TIER2_THRESHOLD_PERCENT", "0.15"),
        "dip_tier3":         env.get("DIP_TIER3_THRESHOLD_PERCENT", "0.22"),
        "recycler_sell_threshold": env.get("RECYCLER_SELL_THRESHOLD_PERCENT", "0.18"),
        "recycler_pool_percent":   env.get("RECYCLER_POOL_PERCENT", "0.35"),
        "max_order_usd":     env.get("MAX_ORDER_USD", "1000.00"),
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
        "PAPER_TRADING":                   body.get("paper_trading"),
        "DIP_THRESHOLD_PERCENT":           body.get("dip_tier1"),
        "DIP_TIER2_THRESHOLD_PERCENT":     body.get("dip_tier2"),
        "DIP_TIER3_THRESHOLD_PERCENT":     body.get("dip_tier3"),
        "RECYCLER_SELL_THRESHOLD_PERCENT": body.get("recycler_sell_threshold"),
        "RECYCLER_POOL_PERCENT":           body.get("recycler_pool_percent"),
        "MAX_ORDER_USD":                   body.get("max_order_usd"),
    }
    updates = {k: str(v) for k, v in allowed.items() if v is not None}
    try:
        _write_env(updates)
        return jsonify({"ok": True, "message": "Settings saved. Bot picks up changes on next cycle (within 5 min)."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Quick Buy (auth required) ────────────────────────────────────────────────

@app.route("/api/buy", methods=["POST"])
@requires_auth
def quick_buy():
    """
    Execute an immediate market buy of BTC.
    Body: { "usd_amount": 50.0 }
    Places a limit buy at 0.05% below current price (same as DCA logic).
    """
    body = request.get_json(force=True, silent=True) or {}
    try:
        usd_amount = float(body.get("usd_amount", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid usd_amount"}), 400

    env = _read_env()
    max_order = float(env.get("MAX_ORDER_USD", "2000"))
    if usd_amount < 5:
        return jsonify({"ok": False, "error": "Minimum buy is $5"}), 400
    if usd_amount > max_order:
        return jsonify({"ok": False, "error": f"Amount exceeds max order size (${max_order})"}), 400

    try:
        # Get current price
        req = urllib.request.Request(
            "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
            headers={"User-Agent": "BotCoin/2.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            ticker = json.loads(resp.read())
        if ticker.get("error"):
            return jsonify({"ok": False, "error": "Could not fetch price from Kraken"}), 500
        current_price = float(list(ticker["result"].values())[0]["c"][0])
        limit_price   = round(current_price * 0.9995, 1)
        btc_amount    = round((usd_amount / limit_price) * 0.9975, 8)  # account for fee

        # Place order
        api_key    = env.get("KRAKEN_API_KEY", "").strip()
        api_secret = env.get("KRAKEN_API_SECRET", "").strip()
        if not api_key or not api_secret:
            return jsonify({"ok": False, "error": "API keys not configured"}), 500

        urlpath = "/0/private/AddOrder"
        nonce   = str(int(time.time() * 1000))
        data    = {
            "nonce":     nonce,
            "ordertype": "limit",
            "type":      "buy",
            "volume":    str(btc_amount),
            "pair":      env.get("TRADING_PAIR", "XBTUSD"),
            "price":     str(limit_price),
            "userref":   99,  # integer tag for quick buy orders
        }
        post = urllib.parse.urlencode(data).encode()
        sig  = _kraken_sign(urlpath, data, api_secret)
        order_req = urllib.request.Request(
            "https://api.kraken.com" + urlpath,
            data=post,
            headers={
                "API-Key":  api_key,
                "API-Sign": sig,
                "Content-Type": "application/x-www-form-urlencoded",
            }
        )
        with urllib.request.urlopen(order_req, timeout=10) as resp:
            result = json.loads(resp.read())

        if result.get("error") and result["error"]:
            return jsonify({"ok": False, "error": ", ".join(result["error"])}), 400

        order_ids = result.get("result", {}).get("txid", [])
        order_id  = order_ids[0] if order_ids else "unknown"

        # Record trade in database via strictly controlled write function
        maker_fee  = float(env.get("KRAKEN_MAKER_FEE", "0.0025"))
        fee_usd    = round(usd_amount * maker_fee, 4)
        actual_btc = round(usd_amount / limit_price, 8)
        _record_quick_buy_trade(order_id, actual_btc, usd_amount, limit_price, fee_usd)

        return jsonify({
            "ok":          True,
            "order_id":    order_id,
            "btc_amount":  actual_btc,
            "usd_amount":  usd_amount,
            "price":       limit_price,
            "message":     f"Bought ~{actual_btc:.8f} BTC at ${limit_price:,.2f}"
        })

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
        "PAPER_TRADING":                   body.get("paper_trading", "false"),
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
    """Return True if version a is greater than version b."""
    try:
        return tuple(int(x) for x in a.split(".")) > tuple(int(x) for x in b.split("."))
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
