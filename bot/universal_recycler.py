"""
universal_recycler.py — Always-on, volatility-adaptive Range Recycler (v2.0).

This generalizes the v1 Sideways overlay (sideways.py): there is no 12%-range
activation gate and no on/off detection. The recycler runs every tick, buying
small slices below a moving reference and selling each slice for a small profit
above its own entry. Bands widen with volatility so the recycler trades less
frequently (and pays fewer fees) in storms and more often when calm.

Per-position model
───────────────────
Each buy opens one row in range_positions carrying its OWN sell target
(sell_band_price), pinned at open-time from the band in force then. Sells are
evaluated per position, so a slice always closes for a profit relative to where
IT was bought — never a blended basis. Positions that age past
RECYCLER_TIME_LIMIT_DAYS (or that are held underwater through a breakdown past
BREAKDOWN_MAX_HOLD_DAYS) are CONVERTED to long-hold: they join the stack and are
never force-sold at a loss.

Regime awareness (from regime_detector)
───────────────────────────────────────
  chop         open new positions + close ripe ones (normal operation)
  breakout_up  do NOT open (don't chase); DO close into strength
  breakdown    do NOT open; HOLD every position (explicit anti-panic-sell)
  cooling      do NOT open yet; DO close ripe ones

This module is pure decision logic: it returns a list of action dicts. main.py's
v2 orchestrator routes every action through the global throttle and the shared
execute_buy/execute_sell path, then updates range_positions. Keeping execution
out of here mirrors how check_sideways feeds run_range_recycler in v1.

Prime directive: net-accumulate sats from chop without ever net-distributing the
stack.
"""

import logging
from datetime import datetime, timezone

from config import Config
from database import (
    get_open_range_positions, count_open_range_positions,
    get_recent_average_price, get_recent_high, get_recent_low,
)
from regime_detector import (
    REGIME_CHOP, REGIME_BREAKOUT_UP, REGIME_BREAKDOWN, REGIME_COOLING,
)

logger = logging.getLogger(__name__)


# Volatility-adaptive band table (impl spec §2 / design doc). Each tuple is
#   (raw_atr_ratio_ceiling, buy_band_fraction, sell_band_fraction)
# evaluated in order; the first row whose ceiling the ratio is at-or-below wins.
# buy band is negative (buy below reference), sell band positive (sell above
# entry). The final row's ceiling is infinity (the Storm bucket).
_VOL_BANDS = (
    (0.8, -0.02, 0.03),   # Calm
    (1.2, -0.03, 0.05),   # Normal
    (1.6, -0.04, 0.06),   # Active (the v1 Sideways default band)
    (float("inf"), -0.06, 0.09),   # Storm
)


def _bands_for(atr_ratio: float) -> tuple[float, float]:
    """
    Return (buy_band, sell_band) fractions for the given RAW ATR ratio.

    Uses the raw atr_pct/baseline_pct ratio (not the clamped vol_multiplier) so
    the Storm bucket (>1.6×) is reachable — see Q-new-4 in the impl spec. A None
    or non-positive ratio falls back to the Normal band.
    """
    if not atr_ratio or atr_ratio <= 0:
        return _VOL_BANDS[1][1], _VOL_BANDS[1][2]
    for ceiling, buy_band, sell_band in _VOL_BANDS:
        if atr_ratio <= ceiling:
            return buy_band, sell_band
    # Unreachable (last ceiling is +inf) but keeps the type-checker honest.
    return _VOL_BANDS[-1][1], _VOL_BANDS[-1][2]


def _vol_state_name(atr_ratio: float) -> str:
    """Human label for the current volatility bucket (for status/logging)."""
    names = ("calm", "normal", "active", "storm")
    if not atr_ratio or atr_ratio <= 0:
        return "normal"
    for (ceiling, _b, _s), name in zip(_VOL_BANDS, names):
        if atr_ratio <= ceiling:
            return name
    return "storm"


def _band_reference_price(cfg: Config, current_price: float) -> float:
    """
    The midpoint the buy band is measured from (Q-new-1, RECYCLER_BAND_REFERENCE).

      vwap_24h       trailing 24h mean intraday price (stable, default)
      mid_recent_hl  midpoint of the recent N-day high/low
      last_close     just the live price (band is then a pure trailing stop)

    Falls back to the live price if the chosen reference has no data yet.
    """
    ref = cfg.recycler_band_reference
    if ref == "vwap_24h":
        avg = get_recent_average_price(hours=24)
        return avg if avg else current_price
    if ref == "mid_recent_hl":
        lookback = cfg.regime_break_lookback_days * 24
        high = get_recent_high(hours=lookback)
        low = get_recent_low(hours=lookback)
        if high and low:
            return (high + low) / 2.0
        return current_price
    # last_close
    return current_price


def _max_positions(cfg: Config, portfolio_usd: float) -> int:
    """
    Cap on concurrent open positions: scales with portfolio size but never below
    the configured floor. Larger portfolios may hold more simultaneous slices.
    """
    scaled = int(portfolio_usd / cfg.recycler_position_divisor)
    return max(cfg.recycler_min_positions, scaled)


def _position_size_usd(cfg: Config, portfolio_usd: float) -> float:
    """Per-position USD size: the smaller of the hard cap and a % of portfolio."""
    return min(cfg.recycler_position_cap_usd,
               cfg.recycler_position_pct * portfolio_usd)


def _age_days(position: dict) -> float:
    """Age of an open position in days, from its row timestamp."""
    ts = position.get("timestamp")
    if not ts:
        return 0.0
    try:
        opened = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return 0.0
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - opened).total_seconds() / 86400.0


def evaluate(cfg: Config, current_price: float, btc_balance: float,
             usd_balance: float, atr_ratio: float, regime: str) -> list[dict]:
    """
    Return the list of recycler actions to take this tick.

    Action dict shapes (consumed by main.py's v2 orchestrator):
      open:    {"type":"buy",  "reason":"universal_recycler_open",
                "usd_amount":..., "sell_band_price":..., "vol_multiplier":...}
      close:   {"type":"sell", "reason":"universal_recycler_close",
                "btc_amount":..., "position_id":...}
      convert: {"type":"convert", "position_id":...}   # DB-only, no order

    No orders are placed here; the caller throttles and executes them.
    """
    actions: list[dict] = []
    buy_band, sell_band = _bands_for(atr_ratio)
    portfolio_usd = btc_balance * current_price + usd_balance

    # ── 1. CLOSE / CONVERT side — evaluated in ALL regimes EXCEPT breakdown,
    #        where every position is held to avoid panic-selling a flush.
    open_positions = get_open_range_positions()
    for pos in open_positions:
        age = _age_days(pos)

        if regime == REGIME_BREAKDOWN:
            # Hold through the flush — but a position underwater longer than the
            # breakdown max-hold is converted to long-hold rather than churned.
            if age >= cfg.breakdown_max_hold_days:
                actions.append({"type": "convert", "position_id": pos["id"],
                                "reason": "breakdown_max_hold"})
            continue

        # Age-out conversion (applies in chop/breakout_up/cooling).
        if age >= cfg.recycler_time_limit_days:
            actions.append({"type": "convert", "position_id": pos["id"],
                            "reason": "time_limit"})
            continue

        sell_target = pos.get("sell_band_price")
        if sell_target is None:
            # Position from before the v2 columns existed — derive its target.
            sell_target = pos["buy_price"] * (1 + sell_band)
        if current_price >= sell_target:
            actions.append({"type": "sell", "reason": "universal_recycler_close",
                            "btc_amount": pos["btc_amount"],
                            "position_id": pos["id"]})

    # ── 2. OPEN side — only in chop. breakout_up/cooling don't chase; breakdown
    #        never opens. Capped by max concurrent positions and min order size.
    if regime == REGIME_CHOP:
        if count_open_range_positions() < _max_positions(cfg, portfolio_usd):
            reference = _band_reference_price(cfg, current_price)
            buy_trigger = reference * (1 + buy_band)
            if current_price <= buy_trigger:
                usd = _position_size_usd(cfg, portfolio_usd)
                affordable = usd_balance - cfg.min_usd_reserve
                usd = min(usd, affordable)
                if usd >= cfg.min_order_usd:
                    actions.append({
                        "type": "buy",
                        "reason": "universal_recycler_open",
                        "usd_amount": usd,
                        "sell_band_price": current_price * (1 + sell_band),
                        "vol_multiplier": atr_ratio,
                    })
                else:
                    logger.debug("[recycler] buy trigger hit but funds short "
                                 "($%.2f available, need $%.2f)",
                                 affordable, cfg.min_order_usd)

    return actions


def get_recycler_status(cfg: Config, current_price: float,
                        atr_ratio: float) -> dict:
    """Build the Universal Recycler block for /api/status (additive, v2-only)."""
    buy_band, sell_band = _bands_for(atr_ratio)
    positions = get_open_range_positions()
    return {
        "open_positions": len(positions),
        "buy_band_pct":   round(buy_band * 100, 2),
        "sell_band_pct":  round(sell_band * 100, 2),
        "vol_state":      _vol_state_name(atr_ratio),
        "band_reference": cfg.recycler_band_reference,
        "positions": [
            {
                "id":              p["id"],
                "buy_price":       round(p["buy_price"], 2),
                "btc_amount":      round(p["btc_amount"], 8),
                "sell_band_price": round(p["sell_band_price"], 2)
                                   if p.get("sell_band_price") else None,
                "age_days":        round(_age_days(p), 1),
            }
            for p in positions
        ],
    }
