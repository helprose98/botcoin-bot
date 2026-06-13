"""
throttle.py — Global anti-thrash trade dampener.

Per-strategy cooldowns (dip = 12h, recycler = 36h) already exist, but they are
reason-scoped: nothing today stops two *different* strategies from firing on the
same tick (e.g. a dip buy plus a sideways range buy), and nothing caps the total
number of trades in a day. In choppy markets that stacking compounds into
death-by-fees, which works directly against the prime directive of ending up with
more BTC long-term.

This module provides a single global guard the loop and the API call before
placing ANY trade:
  - a minimum gap between any two trades, and
  - a maximum number of trades per UTC calendar day.

check_throttle() returns (allowed, message). If not allowed, the caller MUST NOT
place the order and MUST log/surface the reason. record_trade_for_throttle() is
called after a trade is successfully recorded to advance the counters.

State lives in the bot_state key/value table so it survives restarts and is
visible to the read-only API container.
"""

from __future__ import annotations

import time
import logging
from datetime import datetime, timezone
from typing import Tuple

import database as db

logger = logging.getLogger(__name__)

# Reasons that BYPASS the throttle. Cycle-closing trades are time-sensitive —
# they close a round trip while the price is favorable and are already gated by
# their own one-shot waiting_* flags, so they cannot thrash by definition.
# "onboarding" is a DB pseudo-trade, not a real order.
THROTTLE_BYPASS_REASONS = {
    "recycler_rebuy",        # BTC mode cycle close
    "usd_recycler_resell",   # USD mode cycle close
    "onboarding",            # not a real trade
}

# bot_state keys owned by the throttle.
_KEY_LAST_TRADE_TS    = "last_trade_ts"
_KEY_TRADES_TODAY     = "trades_today_count"
_KEY_TRADES_TODAY_DAY = "trades_today_date"


def _today_utc() -> str:
    """Current UTC calendar day as YYYY-MM-DD."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def check_throttle(reason: str, cfg) -> Tuple[bool, str]:
    """
    Return (allowed, message). `allowed=False` means the caller must skip
    placing the order. `message` is a human-readable explanation safe to log
    and surface in the API.

    Cycle-closing reasons bypass the throttle entirely (see
    THROTTLE_BYPASS_REASONS).
    """
    if reason in THROTTLE_BYPASS_REASONS:
        return True, "bypass"

    now = int(time.time())
    today_utc = _today_utc()

    # Daily cap — reset the counter when the UTC day rolls over.
    state_date = db.get_state(_KEY_TRADES_TODAY_DAY) or ""
    if state_date != today_utc:
        db.set_state(_KEY_TRADES_TODAY_DAY, today_utc)
        db.set_state(_KEY_TRADES_TODAY, "0")

    count_today = int(db.get_state(_KEY_TRADES_TODAY) or 0)
    if count_today >= cfg.max_trades_per_day:
        return False, f"daily cap reached ({count_today}/{cfg.max_trades_per_day})"

    # Minimum gap between any two trades.
    last_ts = int(db.get_state(_KEY_LAST_TRADE_TS) or 0)
    elapsed = now - last_ts
    if elapsed < cfg.min_gap_between_trades_seconds:
        remaining = cfg.min_gap_between_trades_seconds - elapsed
        return False, f"min-gap cooldown: {remaining}s remaining"

    return True, "ok"


def record_trade_for_throttle() -> None:
    """
    Advance the throttle counters. Call AFTER a trade is successfully recorded.

    Increments today's trade count (resetting on a UTC day rollover) and stamps
    the last-trade timestamp used by the min-gap guard.
    """
    now = int(time.time())
    today_utc = _today_utc()
    state_date = db.get_state(_KEY_TRADES_TODAY_DAY) or ""
    if state_date != today_utc:
        db.set_state(_KEY_TRADES_TODAY_DAY, today_utc)
        db.set_state(_KEY_TRADES_TODAY, "1")
    else:
        count = int(db.get_state(_KEY_TRADES_TODAY) or 0)
        db.set_state(_KEY_TRADES_TODAY, str(count + 1))
    db.set_state(_KEY_LAST_TRADE_TS, str(now))


def seconds_until_next_allowed(cfg) -> int:
    """
    Seconds remaining before the min-gap permits another trade (0 if allowed
    now). Used to populate the /api/status throttle block.
    """
    last_ts = int(db.get_state(_KEY_LAST_TRADE_TS) or 0)
    elapsed = int(time.time()) - last_ts
    return max(0, cfg.min_gap_between_trades_seconds - elapsed)
