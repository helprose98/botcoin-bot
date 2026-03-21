"""
mode_manager.py — Determines which accumulation mode the bot should run in.

Three modes:
  BTC_ACCUMULATE  — Buy BTC aggressively, sell only to recycle into more BTC.
                    Best during bull markets and accumulation phases.

  USD_ACCUMULATE  — Sell BTC aggressively, buy only to recycle into more USD.
                    Best during bear markets and distribution phases.

  AUTO            — Watches the 200-day moving average (200MA) trend.
                    Switches to BTC_ACCUMULATE when price is above 200MA (bull),
                    switches to USD_ACCUMULATE when price is below 200MA (bear).
                    Includes hysteresis to prevent thrashing on the boundary.

The active mode is persisted in the database so a restart doesn't reset it.
"""

import logging
from enum import Enum
from datetime import datetime, timezone

from database import get_state, set_state, record_price, get_price_history_days

logger = logging.getLogger(__name__)


class Mode(str, Enum):
    BTC_ACCUMULATE = "btc_accumulate"
    USD_ACCUMULATE = "usd_accumulate"
    AUTO           = "auto"


# How far above/below the 200MA price must be before auto mode switches.
# Prevents rapid flip-flopping when price hovers around the MA.
# 0.05 = price must be 5% above MA to confirm bull, 5% below to confirm bear.
AUTO_HYSTERESIS = 0.05

# Minimum days the bot stays in a mode before auto can switch it again.
# Prevents thrashing during sideways markets.
AUTO_MIN_DAYS_IN_MODE = 7


def get_configured_mode(cfg) -> Mode:
    """Return the MODE setting from config."""
    raw = getattr(cfg, "mode", "auto").lower().strip()
    try:
        return Mode(raw)
    except ValueError:
        logger.warning("Unknown MODE '%s', defaulting to AUTO", raw)
        return Mode.AUTO


def get_active_mode(cfg, current_price: float) -> Mode:
    """
    Return the mode the bot should actually be running in right now.

    - If MODE=btc_accumulate or usd_accumulate → always return that mode.
    - If MODE=auto → evaluate trend and return the appropriate mode,
      with hysteresis and minimum-days guard.
    """
    configured = get_configured_mode(cfg)

    if configured != Mode.AUTO:
        # Manual override — just return it, log if it changed
        _persist_mode(configured, reason="manual config")
        return configured

    # AUTO mode — evaluate the 200-day MA
    return _evaluate_auto_mode(current_price)


def _evaluate_auto_mode(current_price: float) -> Mode:
    """
    Evaluate whether we're in a bull or bear trend using the 200-day MA.
    Returns the appropriate mode with hysteresis protection.
    """
    ma200 = calculate_200ma()

    if ma200 is None:
        # Not enough price history yet (need ~200 days of samples)
        # Default to BTC accumulate while building history — conservative choice
        days_of_history = _days_of_price_history()
        logger.info(
            "Auto mode: insufficient price history (%d days, need 200). "
            "Defaulting to BTC_ACCUMULATE.", days_of_history
        )
        _persist_mode(Mode.BTC_ACCUMULATE,
                      reason=f"auto_insufficient_history_{days_of_history}d")
        return Mode.BTC_ACCUMULATE

    deviation = (current_price - ma200) / ma200  # positive = above MA, negative = below

    current_mode_str = get_state("active_mode", Mode.BTC_ACCUMULATE.value)
    current_mode = Mode(current_mode_str)
    last_switch_str = get_state("mode_last_switch_timestamp")

    # Check minimum days guard
    if last_switch_str:
        last_switch = datetime.fromisoformat(last_switch_str)
        days_in_mode = (datetime.now(timezone.utc) - last_switch).days
        if days_in_mode < AUTO_MIN_DAYS_IN_MODE:
            logger.debug(
                "Auto mode: only %d days since last switch (min %d). "
                "Staying in %s.", days_in_mode, AUTO_MIN_DAYS_IN_MODE, current_mode.value
            )
            return current_mode

    # Determine what the trend says
    if deviation >= AUTO_HYSTERESIS:
        # Price is meaningfully ABOVE 200MA → bull trend → accumulate BTC
        new_mode = Mode.BTC_ACCUMULATE
        trend_desc = f"BULL (price ${current_price:,.0f} is {deviation*100:.1f}% above 200MA ${ma200:,.0f})"
    elif deviation <= -AUTO_HYSTERESIS:
        # Price is meaningfully BELOW 200MA → bear trend → accumulate USD
        new_mode = Mode.USD_ACCUMULATE
        trend_desc = f"BEAR (price ${current_price:,.0f} is {abs(deviation)*100:.1f}% below 200MA ${ma200:,.0f})"
    else:
        # Within hysteresis band — stay in current mode
        logger.debug(
            "Auto mode: price is within %.1f%% of 200MA (hysteresis band). "
            "Staying in %s.", AUTO_HYSTERESIS * 100, current_mode.value
        )
        return current_mode

    if new_mode != current_mode:
        logger.info("=" * 60)
        logger.info("AUTO MODE SWITCH: %s → %s", current_mode.value.upper(), new_mode.value.upper())
        logger.info("Trend: %s", trend_desc)
        logger.info("=" * 60)
        _persist_mode(new_mode, reason=f"auto_trend_{trend_desc[:40]}")
    else:
        logger.debug("Auto mode: trend=%s mode=%s (no change)", trend_desc, new_mode.value)

    return new_mode


def _persist_mode(mode: Mode, reason: str = ""):
    """Save the active mode to the database."""
    previous = get_state("active_mode", "none")
    if previous != mode.value:
        set_state("active_mode", mode.value)
        set_state("mode_last_switch_timestamp",
                  datetime.now(timezone.utc).isoformat())
        set_state("mode_last_switch_reason", reason)
        logger.info("Mode persisted: %s (reason: %s)", mode.value, reason)


def calculate_200ma() -> float | None:
    """
    Calculate the 200-day simple moving average from stored price history.
    Returns None if insufficient data exists.
    Uses daily close prices (one sample per day, taken at the daily sample time).
    """
    prices = get_price_history_days(200)
    if len(prices) < 100:  # Require at least 100 days before trusting the MA
        return None
    ma = sum(prices) / len(prices)
    logger.debug("200MA calculated from %d days of data: $%.2f", len(prices), ma)
    return ma


def _days_of_price_history() -> int:
    """Return how many days of price history we have."""
    prices = get_price_history_days(250)
    return len(prices)


def get_mode_status(current_price: float) -> dict:
    """Return a dict describing current mode status for logging/reporting."""
    ma200 = calculate_200ma()
    active = get_state("active_mode", Mode.BTC_ACCUMULATE.value)
    last_switch = get_state("mode_last_switch_timestamp", "never")
    reason = get_state("mode_last_switch_reason", "")
    days_history = _days_of_price_history()

    status = {
        "active_mode":    active,
        "last_switch":    last_switch,
        "switch_reason":  reason,
        "price_200ma":    round(ma200, 2) if ma200 else None,
        "ma_deviation_pct": round(((current_price - ma200) / ma200) * 100, 2) if ma200 else None,
        "days_of_history": days_history,
        "ma_available":   ma200 is not None,
    }
    return status
