"""
snapshots.py — Daily portfolio snapshot writer (v2.2.0).

Fires once per UTC day from the main trading loop (piggy-backed on the
~5-minute tick).  Writes a single row to `daily_snapshots` capturing the
full portfolio state at that moment.  The write is idempotent: running
twice on the same UTC date updates the existing row rather than inserting
a duplicate (INSERT … ON CONFLICT DO UPDATE).

This module is **observability only**.  It touches no order logic, no
balances, and makes no trades.  A snapshot failure must NEVER crash the
trading loop — the caller in main.py wraps _maybe_write_daily_snapshot()
in try/except and logs a warning on error.
"""

import logging
from datetime import datetime, timezone

from config import load_config
from database import (
    get_all_trades,
    get_all_deposits,
    get_last_daily_snapshot_date,
    get_latest_snapshot,
    get_state,
    upsert_daily_snapshot,
)
from mode_manager import get_operating_regime

logger = logging.getLogger(__name__)

# ── UTC cutoff: only write on the first tick at or after 00:05 UTC ───────────
_SNAPSHOT_AFTER_MINUTE_UTC = 5  # do not write before HH:05 UTC


def _current_utc_date_str() -> str:
    """Return today's UTC date as YYYY-MM-DD."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _current_utc_ts() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _assemble_snapshot_row(snapshot_date_utc: str, snapshot_ts: str) -> dict:
    """Gather all fields needed for a daily_snapshots row.

    Reads live balances from the latest portfolio_snapshot, derives aggregates
    from the trades table, and fetches deposit totals via get_all_deposits.
    """
    cfg = load_config()

    # ── BTC price — latest intraday sample ───────────────────────────────────
    # Import here to avoid circular imports; price_history is a small module.
    from database import get_connection  # noqa: PLC0415
    with get_connection() as conn:
        price_row = conn.execute(
            "SELECT price_usd FROM price_history ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        trade_rows = conn.execute(
            "SELECT side, btc_amount, usd_amount, fee_usd, reason "
            "FROM trades"
        ).fetchall()

    btc_price_usd = float(price_row["price_usd"]) if price_row else 0.0

    # ── Balances from latest portfolio snapshot ───────────────────────────────
    snapshot = get_latest_snapshot()
    btc_stack = float(snapshot["btc_balance"]) if snapshot else 0.0
    usd_reserve = float(snapshot["usd_balance"]) if snapshot else 0.0

    # ── Avg cost basis — volume-weighted buy average (mirrors /api/status) ───
    all_buys = [t for t in trade_rows if t["side"] == "buy"]
    buys_btc = sum(t["btc_amount"] for t in all_buys)
    buys_cost_usd = sum(t["usd_amount"] + t["fee_usd"] for t in all_buys)
    avg_cost_basis = (buys_cost_usd / buys_btc) if buys_btc > 0 else (
        float(snapshot["avg_cost_basis"]) if snapshot else 0.0
    )

    # ── Total deposits (USD-equivalent) — use existing deposits table ────────
    all_deposits = get_all_deposits()
    total_deposits_usd = sum(
        float(d["usd_value_at_time"]) for d in all_deposits
    )

    # ── Total DCA spend — cumulative buy-side USD (includes fees) ─────────────
    total_dca_usd = sum(
        t["usd_amount"] + t["fee_usd"]
        for t in trade_rows
        if t["side"] == "buy" and t["reason"] != "onboarding"
    )

    # ── Trade count — non-onboarding trades only ──────────────────────────────
    trade_count = sum(
        1 for t in trade_rows if t["reason"] != "onboarding"
    )

    # ── Total portfolio value ─────────────────────────────────────────────────
    total_value_usd = btc_stack * btc_price_usd + usd_reserve

    # ── Operating regime — Accumulate / Neutral / Harvest ────────────────────
    regime = get_operating_regime(cfg, btc_price_usd).value

    # ── Aggression level — dial preset stored in bot_state, defaulting to
    # "standard" when not yet set (matches dashboard default) ─────────────────
    aggression_level = get_state("aggression_level", "standard") or "standard"

    return {
        "snapshot_date_utc": snapshot_date_utc,
        "snapshot_ts":       snapshot_ts,
        "btc_stack":         btc_stack,
        "usd_reserve":       usd_reserve,
        "btc_price_usd":     btc_price_usd,
        "total_value_usd":   total_value_usd,
        "avg_cost_basis":    avg_cost_basis,
        "total_deposits_usd": total_deposits_usd,
        "total_dca_usd":     total_dca_usd,
        "trade_count":       trade_count,
        "regime":            regime,
        "aggression_level":  aggression_level,
        "notes":             None,
    }


def _maybe_write_daily_snapshot() -> None:
    """Write a daily snapshot row if today's UTC date is not yet recorded.

    Checks:
    1. Current UTC minute must be >= _SNAPSHOT_AFTER_MINUTE_UTC (00:05) to
       avoid writing on the very first tick of the day before the bot has had
       a chance to do its normal work.
    2. If today's date is already in daily_snapshots → no-op (idempotent).
    3. Otherwise → assemble and upsert the row.

    This function is called at the end of every main-loop tick and must
    complete quickly.  The caller wraps it in try/except so any error here
    produces a warning log but never blocks the trading loop.
    """
    now_utc = datetime.now(timezone.utc)

    # Guard: do not write before 00:05 UTC on a given day.
    if now_utc.hour == 0 and now_utc.minute < _SNAPSHOT_AFTER_MINUTE_UTC:
        return

    snapshot_date_utc = now_utc.strftime("%Y-%m-%d")
    last_date = get_last_daily_snapshot_date()

    if last_date == snapshot_date_utc:
        # Already written today — nothing to do.
        return

    snapshot_ts = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    row = _assemble_snapshot_row(snapshot_date_utc, snapshot_ts)

    upsert_daily_snapshot(
        snapshot_date_utc=row["snapshot_date_utc"],
        snapshot_ts=row["snapshot_ts"],
        btc_stack=row["btc_stack"],
        usd_reserve=row["usd_reserve"],
        btc_price_usd=row["btc_price_usd"],
        total_value_usd=row["total_value_usd"],
        avg_cost_basis=row["avg_cost_basis"],
        total_deposits_usd=row["total_deposits_usd"],
        total_dca_usd=row["total_dca_usd"],
        trade_count=row["trade_count"],
        regime=row["regime"],
        aggression_level=row["aggression_level"],
        notes=row["notes"],
    )
    logger.info(
        "daily_snapshot written: date=%s btc_stack=%.8f usd_reserve=%.2f "
        "price=%.2f total_value=%.2f regime=%s",
        snapshot_date_utc,
        row["btc_stack"],
        row["usd_reserve"],
        row["btc_price_usd"],
        row["total_value_usd"],
        row["regime"],
    )
