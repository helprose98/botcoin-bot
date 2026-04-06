"""
sideways.py — Sideways Market detection and Range Recycler trading logic.

Sideways Market is an OVERLAY, not a mode. It runs on top of whatever
Auto mode has selected (BTC accumulate or USD accumulate).

Detection:
  - Calculate 14-day rolling high/low from daily price history
  - range_pct = ((high - low) / low) * 100
  - If range_pct < 12% → sideways market detected
  - Must stay sideways for 2 consecutive checks before activating

Range Recycler (when sideways is active):
  - BTC mode: buy at -4% from midpoint, sell at +6% from buy price
  - USD mode: sell at +4% from midpoint, buy at -6% from sell price
  - Max 5 concurrent positions (configurable)
  - Fixed thresholds — aggressiveness slider does NOT affect these

Exit strategy:
  - When sideways ends (range > 12%), pause new trades
  - Existing open positions become normal recycler positions
"""

import logging
from datetime import datetime, timezone

from config import Config
from database import (
    get_price_history_days, get_state, set_state,
    get_open_range_positions, count_open_range_positions,
    add_range_position, close_range_position, convert_range_positions,
)
from mode_manager import Mode

logger = logging.getLogger(__name__)

# Fixed thresholds from backtesting — not affected by aggressiveness slider
RANGE_WINDOW_DAYS     = 14
RANGE_THRESHOLD_PCT   = 12.0
BUY_THRESHOLD_PCT     = -4.0   # buy when price is 4% below midpoint
SELL_THRESHOLD_PCT    = 6.0    # sell when position is 6% above buy price
MIN_ACTIVATION_CHECKS = 2      # must be sideways for 2 consecutive checks


def detect_sideways() -> dict:
    """
    Detect whether the market is in a sideways range.

    Returns dict with:
      - is_sideways: bool
      - range_pct: float or None
      - range_high: float or None
      - range_low: float or None
      - range_midpoint: float or None
    """
    prices = get_price_history_days(RANGE_WINDOW_DAYS)

    if len(prices) < RANGE_WINDOW_DAYS:
        logger.debug("Sideways: insufficient price history (%d/%d days)",
                     len(prices), RANGE_WINDOW_DAYS)
        return {
            "is_sideways": False,
            "range_pct": None,
            "range_high": None,
            "range_low": None,
            "range_midpoint": None,
        }

    range_high = max(prices)
    range_low = min(prices)

    if range_low <= 0:
        return {
            "is_sideways": False,
            "range_pct": None,
            "range_high": range_high,
            "range_low": range_low,
            "range_midpoint": None,
        }

    range_pct = ((range_high - range_low) / range_low) * 100
    midpoint = (range_high + range_low) / 2

    return {
        "is_sideways": range_pct < RANGE_THRESHOLD_PCT,
        "range_pct": round(range_pct, 2),
        "range_high": round(range_high, 2),
        "range_low": round(range_low, 2),
        "range_midpoint": round(midpoint, 2),
    }


def update_sideways_state(detection: dict) -> bool:
    """
    Update sideways activation state with hysteresis.
    Must be sideways for MIN_ACTIVATION_CHECKS consecutive checks to activate.
    Returns True if sideways is currently active.
    """
    was_active = get_state("sideways_active", "false") == "true"
    consecutive = int(get_state("sideways_consecutive_checks", "0") or 0)

    if detection["is_sideways"]:
        consecutive += 1
        set_state("sideways_consecutive_checks", str(consecutive))

        if consecutive >= MIN_ACTIVATION_CHECKS and not was_active:
            set_state("sideways_active", "true")
            set_state("sideways_activated_at",
                      datetime.now(timezone.utc).isoformat())
            logger.info("=" * 60)
            logger.info("SIDEWAYS MARKET ACTIVATED | range=%.1f%% (<%d%%)",
                        detection["range_pct"], RANGE_THRESHOLD_PCT)
            logger.info("  14d high=$%.2f  low=$%.2f  midpoint=$%.2f",
                        detection["range_high"], detection["range_low"],
                        detection["range_midpoint"])
            logger.info("=" * 60)
            return True

        return was_active

    else:
        # Range broke — deactivate
        set_state("sideways_consecutive_checks", "0")

        if was_active:
            set_state("sideways_active", "false")
            converted = convert_range_positions()
            logger.info("=" * 60)
            logger.info("SIDEWAYS MARKET ENDED | range=%.1f%% (>%d%%)",
                        detection["range_pct"] or 0, RANGE_THRESHOLD_PCT)
            logger.info("  %d open positions converted to normal recycler", converted)
            logger.info("=" * 60)

        return False


def check_sideways(cfg: Config, current_price: float,
                   active_mode: Mode) -> list[dict]:
    """
    Run the full sideways market check cycle.
    Returns a list of actions (buy/sell dicts) to execute.

    Called from the main loop every tick. This function:
      1. Runs detection
      2. Updates activation state
      3. If active, checks for Range Recycler buy/sell opportunities
    """
    if not cfg.sideways_enabled:
        return []

    detection = detect_sideways()
    is_active = update_sideways_state(detection)

    # Persist detection data for API
    if detection["range_pct"] is not None:
        set_state("sideways_range_pct", str(detection["range_pct"]))
    if detection["range_midpoint"] is not None:
        set_state("sideways_range_midpoint", str(detection["range_midpoint"]))

    if not is_active:
        return []

    actions = []

    # Check sells first (close profitable positions)
    sell_action = _check_range_sells(current_price, active_mode)
    if sell_action:
        actions.append(sell_action)

    # Then check buys (open new positions)
    buy_action = _check_range_buys(cfg, current_price, detection, active_mode)
    if buy_action:
        actions.append(buy_action)

    return actions


def _check_range_buys(cfg: Config, current_price: float,
                      detection: dict, active_mode: Mode) -> dict | None:
    """
    Check if we should open a new range recycler position.

    BTC mode: buy when price drops 4% below midpoint
    USD mode: sell when price rises 4% above midpoint (inverted)
    """
    midpoint = detection["range_midpoint"]
    if not midpoint or midpoint <= 0:
        return None

    open_count = count_open_range_positions()
    if open_count >= cfg.range_max_positions:
        logger.debug("Range Recycler: max positions reached (%d/%d)",
                     open_count, cfg.range_max_positions)
        return None

    if active_mode == Mode.BTC_ACCUMULATE:
        # Buy on dips — price must be 4% below midpoint
        deviation_pct = ((current_price - midpoint) / midpoint) * 100
        if deviation_pct > BUY_THRESHOLD_PCT:
            logger.debug("Range Recycler buy: price %.1f%% from midpoint (need %.1f%%)",
                         deviation_pct, BUY_THRESHOLD_PCT)
            return None

        logger.info("Range Recycler BUY: price $%.2f is %.1f%% below midpoint $%.2f",
                    current_price, abs(deviation_pct), midpoint)
        return {
            "type": "buy",
            "usd_amount": cfg.range_trade_size_usd,
            "reason": "range_recycler_buy",
            "range_midpoint": midpoint,
            "deviation_pct": deviation_pct,
        }

    elif active_mode == Mode.USD_ACCUMULATE:
        # Sell on pops — price must be 4% above midpoint
        deviation_pct = ((current_price - midpoint) / midpoint) * 100
        if deviation_pct < abs(BUY_THRESHOLD_PCT):
            logger.debug("Range Recycler sell: price %.1f%% from midpoint (need +%.1f%%)",
                         deviation_pct, abs(BUY_THRESHOLD_PCT))
            return None

        # Sell a BTC amount equivalent to range_trade_size_usd
        btc_to_sell = cfg.range_trade_size_usd / current_price

        logger.info("Range Recycler SELL: price $%.2f is +%.1f%% above midpoint $%.2f",
                    current_price, deviation_pct, midpoint)
        return {
            "type": "sell",
            "btc_amount": btc_to_sell,
            "reason": "range_recycler_sell",
            "range_midpoint": midpoint,
            "deviation_pct": deviation_pct,
        }

    return None


def _check_range_sells(current_price: float,
                       active_mode: Mode) -> dict | None:
    """
    Check open range positions for profit-taking opportunities.

    BTC mode: sell when position is +6% above buy price
    USD mode: buy when position is -6% below sell price (inverted)
    """
    positions = get_open_range_positions()
    if not positions:
        return None

    for pos in positions:
        buy_price = pos["buy_price"]

        if active_mode == Mode.BTC_ACCUMULATE:
            # Sell when +6% above buy price
            gain_pct = ((current_price - buy_price) / buy_price) * 100
            if gain_pct >= SELL_THRESHOLD_PCT:
                logger.info("Range Recycler SELL: position #%d +%.1f%% (bought $%.2f, now $%.2f)",
                            pos["id"], gain_pct, buy_price, current_price)
                return {
                    "type": "sell",
                    "btc_amount": pos["btc_amount"],
                    "reason": "range_recycler_sell",
                    "position_id": pos["id"],
                    "gain_pct": gain_pct,
                }

        elif active_mode == Mode.USD_ACCUMULATE:
            # Buy back when -6% below sell price
            drop_pct = ((buy_price - current_price) / buy_price) * 100
            if drop_pct >= SELL_THRESHOLD_PCT:
                usd_amount = pos["usd_amount"]
                logger.info("Range Recycler BUY: position #%d -%.1f%% (sold $%.2f, now $%.2f)",
                            pos["id"], drop_pct, buy_price, current_price)
                return {
                    "type": "buy",
                    "usd_amount": usd_amount,
                    "reason": "range_recycler_buy",
                    "position_id": pos["id"],
                    "drop_pct": drop_pct,
                }

    return None


def get_sideways_status(cfg: Config) -> dict:
    """
    Return the current sideways state for the API response.
    Always returns a complete object — dashboard expects it.
    """
    is_active = get_state("sideways_active", "false") == "true"
    range_pct = get_state("sideways_range_pct")
    open_positions = count_open_range_positions()

    return {
        "active":             is_active and cfg.sideways_enabled,
        "enabled":            cfg.sideways_enabled,
        "range_pct":          float(range_pct) if range_pct else None,
        "window_days":        RANGE_WINDOW_DAYS,
        "threshold_pct":      RANGE_THRESHOLD_PCT,
        "positions":          open_positions,
        "max_positions":      cfg.range_max_positions,
        "buy_threshold_pct":  BUY_THRESHOLD_PCT,
        "sell_threshold_pct": SELL_THRESHOLD_PCT,
    }
