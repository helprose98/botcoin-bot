"""
database.py — SQLite database setup and all data access functions.
All persistent state lives here: trades, cost basis, price history, bot state,
mode switch log.
"""

import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path("/app/data/bot.db")


def get_connection():
    """Return a database connection with row factory enabled."""
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create all tables if they don't exist. Safe to run on every startup."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        conn.executescript("""
            -- All executed trades (buys and sells)
            CREATE TABLE IF NOT EXISTS trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       DATETIME NOT NULL DEFAULT (datetime('now')),
                order_id        TEXT UNIQUE,
                side            TEXT NOT NULL,         -- 'buy' or 'sell'
                reason          TEXT NOT NULL,         -- 'dca','dip_buy','recycler_sell','recycler_rebuy',
                                                       -- 'usd_dca_sell','usd_spike_sell','usd_recycler_buy',
                                                       -- 'usd_recycler_resell','onboarding'
                btc_amount      REAL NOT NULL,
                usd_amount      REAL NOT NULL,
                price_usd       REAL NOT NULL,
                fee_usd         REAL NOT NULL DEFAULT 0,
                net_usd         REAL NOT NULL,
                active_mode     TEXT NOT NULL DEFAULT 'btc_accumulate',
                paper_trade     INTEGER NOT NULL DEFAULT 0
            );

            -- Running portfolio state snapshot after each trade
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       DATETIME NOT NULL DEFAULT (datetime('now')),
                trade_id        INTEGER REFERENCES trades(id),
                btc_balance     REAL NOT NULL,
                usd_balance     REAL NOT NULL,
                avg_cost_basis  REAL NOT NULL,
                total_fees_paid REAL NOT NULL DEFAULT 0
            );

            -- Daily price samples (one per day for 200MA calculation)
            CREATE TABLE IF NOT EXISTS daily_prices (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                date            TEXT NOT NULL UNIQUE,   -- YYYY-MM-DD
                price_usd       REAL NOT NULL,
                timestamp       DATETIME NOT NULL DEFAULT (datetime('now'))
            );

            -- Intraday price history (sampled every 15 min for dip detection)
            CREATE TABLE IF NOT EXISTS price_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       DATETIME NOT NULL DEFAULT (datetime('now')),
                price_usd       REAL NOT NULL
            );

            -- Mode switch log
            CREATE TABLE IF NOT EXISTS mode_switches (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       DATETIME NOT NULL DEFAULT (datetime('now')),
                from_mode       TEXT,
                to_mode         TEXT NOT NULL,
                reason          TEXT,
                price_usd       REAL,
                ma200           REAL
            );

            -- Bot state (key/value store)
            CREATE TABLE IF NOT EXISTS bot_state (
                key             TEXT PRIMARY KEY,
                value           TEXT,
                updated_at      DATETIME NOT NULL DEFAULT (datetime('now'))
            );

            -- Range Recycler positions (Sideways Market overlay)
            CREATE TABLE IF NOT EXISTS range_positions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       DATETIME NOT NULL DEFAULT (datetime('now')),
                trade_id        INTEGER REFERENCES trades(id),
                buy_price       REAL NOT NULL,
                btc_amount      REAL NOT NULL,
                usd_amount      REAL NOT NULL,
                status          TEXT NOT NULL DEFAULT 'open'  -- 'open', 'closed', 'converted'
            );

            -- Indexes
            CREATE INDEX IF NOT EXISTS idx_price_history_ts  ON price_history(timestamp);
            CREATE INDEX IF NOT EXISTS idx_daily_prices_date ON daily_prices(date);
            CREATE INDEX IF NOT EXISTS idx_trades_ts         ON trades(timestamp);
            CREATE INDEX IF NOT EXISTS idx_trades_reason     ON trades(reason);
            CREATE INDEX IF NOT EXISTS idx_trades_mode       ON trades(active_mode);
            CREATE INDEX IF NOT EXISTS idx_range_positions_status ON range_positions(status);
        """)
    logger.info("Database initialized at %s", DB_PATH)


# ── Trade functions ──────────────────────────────────────────────────────────

def record_trade(order_id, side, reason, btc_amount, usd_amount, price_usd,
                 fee_usd, active_mode="btc_accumulate", paper_trade=False):
    """Insert a completed trade and return its row id."""
    net_usd = usd_amount + fee_usd if side == "buy" else usd_amount - fee_usd
    with get_connection() as conn:
        cur = conn.execute("""
            INSERT INTO trades (order_id, side, reason, btc_amount, usd_amount,
                                price_usd, fee_usd, net_usd, active_mode, paper_trade)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (order_id, side, reason, btc_amount, usd_amount, price_usd,
              fee_usd, net_usd, active_mode, 1 if paper_trade else 0))
        trade_id = cur.lastrowid
    logger.info("[TRADE] %s %s BTC @ $%.2f | reason=%s | mode=%s | fee=$%.4f",
                side.upper(), btc_amount, price_usd, reason, active_mode, fee_usd)
    return trade_id


def get_all_trades():
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM trades ORDER BY timestamp ASC"
        ).fetchall()


def get_last_trade_by_reason(reason):
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM trades WHERE reason=? ORDER BY timestamp DESC LIMIT 1",
            (reason,)
        ).fetchone()


def get_trades_by_mode(mode: str):
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM trades WHERE active_mode=? ORDER BY timestamp ASC",
            (mode,)
        ).fetchall()


# ── Portfolio snapshot functions ─────────────────────────────────────────────

def save_portfolio_snapshot(trade_id, btc_balance, usd_balance,
                             avg_cost_basis, total_fees_paid):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO portfolio_snapshots
                (trade_id, btc_balance, usd_balance, avg_cost_basis, total_fees_paid)
            VALUES (?, ?, ?, ?, ?)
        """, (trade_id, btc_balance, usd_balance, avg_cost_basis, total_fees_paid))


def get_latest_snapshot():
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()


# ── Price history functions ───────────────────────────────────────────────────

def record_price(price_usd: float):
    """Record an intraday price sample."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO price_history (price_usd) VALUES (?)", (price_usd,)
        )


def record_daily_price(price_usd: float):
    """
    Record today's price for 200MA calculation.
    Only one entry per calendar day (UPSERT — last write wins).
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO daily_prices (date, price_usd)
            VALUES (?, ?)
            ON CONFLICT(date) DO UPDATE SET price_usd=excluded.price_usd,
                                            timestamp=datetime('now')
        """, (today, price_usd))


def get_price_history_days(days: int) -> list[float]:
    """
    Return up to `days` daily price samples, oldest first.
    Used for 200MA calculation.
    """
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT price_usd FROM daily_prices
            ORDER BY date DESC LIMIT ?
        """, (days,)).fetchall()
    prices = [r["price_usd"] for r in reversed(rows)]
    return prices


def get_recent_high(hours: int = 168) -> float | None:
    """Return the highest intraday price in the last N hours (default 7 days)."""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT MAX(price_usd) as high FROM price_history
            WHERE timestamp >= datetime('now', ? || ' hours')
        """, (f"-{hours}",)).fetchone()
    return row["high"] if row and row["high"] else None


def get_recent_low(hours: int = 168) -> float | None:
    """Return the lowest intraday price in the last N hours."""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT MIN(price_usd) as low FROM price_history
            WHERE timestamp >= datetime('now', ? || ' hours')
        """, (f"-{hours}",)).fetchone()
    return row["low"] if row and row["low"] else None


# ── Mode switch log ───────────────────────────────────────────────────────────

def record_mode_switch(from_mode, to_mode, reason, price_usd, ma200=None):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO mode_switches (from_mode, to_mode, reason, price_usd, ma200)
            VALUES (?, ?, ?, ?, ?)
        """, (from_mode, to_mode, reason, price_usd, ma200))
    logger.info("Mode switch logged: %s → %s | price=$%.2f | reason=%s",
                from_mode, to_mode, price_usd, reason)


def get_mode_switch_history():
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM mode_switches ORDER BY timestamp ASC"
        ).fetchall()


# ── Bot state functions ───────────────────────────────────────────────────────

def set_state(key: str, value):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO bot_state (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                           updated_at=excluded.updated_at
        """, (key, str(value)))


def get_state(key: str, default=None):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM bot_state WHERE key=?", (key,)
        ).fetchone()
    return row["value"] if row else default


# ── Range positions (Sideways Market) ────────────────────────────────────────

def add_range_position(trade_id: int, buy_price: float,
                       btc_amount: float, usd_amount: float):
    """Record a new open range recycler position."""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO range_positions (trade_id, buy_price, btc_amount, usd_amount, status)
            VALUES (?, ?, ?, ?, 'open')
        """, (trade_id, buy_price, btc_amount, usd_amount))
    logger.info("[RANGE] New position: %.8f BTC @ $%.2f ($%.2f)",
                btc_amount, buy_price, usd_amount)


def get_open_range_positions() -> list[dict]:
    """Return all open range recycler positions."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM range_positions WHERE status='open' ORDER BY timestamp ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def close_range_position(position_id: int):
    """Mark a range position as closed (sold for profit)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE range_positions SET status='closed' WHERE id=?",
            (position_id,)
        )
    logger.info("[RANGE] Position %d closed", position_id)


def convert_range_positions():
    """Convert all open range positions to 'converted' (sideways ended, becomes normal recycler)."""
    with get_connection() as conn:
        count = conn.execute(
            "UPDATE range_positions SET status='converted' WHERE status='open'"
        ).rowcount
    if count:
        logger.info("[RANGE] Converted %d open positions to normal recycler", count)
    return count


def count_open_range_positions() -> int:
    """Return count of open range recycler positions."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM range_positions WHERE status='open'"
        ).fetchone()
    return row["c"] if row else 0


# ── Reporting ────────────────────────────────────────────────────────────────

def get_summary() -> dict:
    """Return a full performance summary across both modes."""
    with get_connection() as conn:
        trades   = conn.execute("SELECT * FROM trades ORDER BY timestamp ASC").fetchall()
        snapshot = conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        switches = conn.execute(
            "SELECT COUNT(*) as c FROM mode_switches"
        ).fetchone()

    total_btc_bought = sum(t["btc_amount"] for t in trades if t["side"] == "buy")
    total_btc_sold   = sum(t["btc_amount"] for t in trades if t["side"] == "sell")
    total_usd_spent  = sum(t["net_usd"]    for t in trades if t["side"] == "buy")
    total_usd_recvd  = sum(t["net_usd"]    for t in trades if t["side"] == "sell")
    total_fees       = sum(t["fee_usd"]    for t in trades)

    return {
        "trade_count":          len(trades),
        "total_btc_bought":     total_btc_bought,
        "total_btc_sold":       total_btc_sold,
        "net_btc_from_bot":     total_btc_bought - total_btc_sold,
        "total_usd_spent":      total_usd_spent,
        "total_usd_received":   total_usd_recvd,
        "net_usd_from_bot":     total_usd_recvd - total_usd_spent,
        "total_fees_paid":      total_fees,
        "mode_switches":        switches["c"] if switches else 0,
        "avg_cost_basis":       snapshot["avg_cost_basis"]   if snapshot else 0,
        "current_btc_balance":  snapshot["btc_balance"]      if snapshot else 0,
        "current_usd_balance":  snapshot["usd_balance"]      if snapshot else 0,
    }
