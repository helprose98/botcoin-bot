"""
harvest.py — Confirmed-rally profit-taking (Strategy v2.0).

Harvest is the ONLY net stack-reduction path in v2. Everything else either buys
BTC or cycles capital round-trip; only Harvest deliberately ends a rally holding
fewer sats and more dry powder, to fund cheaper re-entry later.

It is gated hard, because uncontrolled selling into "strength" is exactly what
drained the stack in v1.x:

  • Trend gate    price ≥ 200MA × HARVEST_THRESHOLD_PCT (a real rally, not a poke)
  • Sustain gate  that condition held for HARVEST_SUSTAIN_DAYS (anti-whipsaw)
  • Profit gate   only sells slices that are HARVEST_TIER_TRIGGER_PCT above basis
  • Per-fire cap  one fire sells at most HARVEST_FIRE_CAP_PCT of the stack
  • Per-rally cap cumulative sells across one rally ≤ HARVEST_TOTAL_CAP_PCT of the
                  stack measured at the rally's start (the structural backstop)
  • Stack floor   suppressed entirely below HARVEST_MIN_STACK_BTC, so a near-zero
                  rebuild stack is never harvested
  • Exit          turns off below 200MA × HARVEST_EXIT_PCT; the rally counters
                  reset so the next rally starts fresh

In a breakout_up regime the trend gate is relaxed to the exit ratio so the bot
can begin trimming earlier in a violent move; all caps still apply.

This module returns at most one action dict per tick (a single sell) or None.
main.py's v2 orchestrator throttles and executes it.

Prime directive: take SOME profit in confirmed rallies to buy back cheaper —
never distribute the bulk of the stack.
"""

import logging
from datetime import datetime, timezone

from config import Config
from database import get_state, set_state
from regime_detector import REGIME_BREAKOUT_UP

logger = logging.getLogger(__name__)


# bot_state keys owned by this module.
_STATE_THRESHOLD_SINCE = "harvest_threshold_since_ts"  # when gate first crossed
_STATE_RALLY_ACTIVE    = "harvest_rally_active"        # "true"/"false"
_STATE_RALLY_START_BTC = "harvest_rally_start_stack"   # stack at rally start
_STATE_RALLY_SOLD_BTC  = "harvest_rally_sold_btc"      # cumulative sold this rally


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _days_since(state_key: str) -> float:
    """Days since the timestamp under state_key, or +inf if never set."""
    ts = get_state(state_key)
    if not ts:
        return float("inf")
    try:
        since = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return float("inf")
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return (_now() - since).total_seconds() / 86400.0


def _float_state(key: str, default: float) -> float:
    """Read a float from bot_state with a safe fallback."""
    raw = get_state(key, str(default))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _end_rally() -> None:
    """Reset all rally bookkeeping — called when price falls back to the exit."""
    if get_state(_STATE_RALLY_ACTIVE, "false") == "true":
        logger.info("[HARVEST] Rally ended — resetting harvest counters")
    set_state(_STATE_RALLY_ACTIVE, "false")
    set_state(_STATE_THRESHOLD_SINCE, "")
    set_state(_STATE_RALLY_SOLD_BTC, "0")


def _begin_rally_if_new(btc_balance: float) -> None:
    """
    Mark the start of a rally the first time harvesting actually engages, and
    snapshot the stack so the per-rally cap is measured against the rally-start
    size (not the shrinking live balance).
    """
    if get_state(_STATE_RALLY_ACTIVE, "false") != "true":
        set_state(_STATE_RALLY_ACTIVE, "true")
        set_state(_STATE_RALLY_START_BTC, str(btc_balance))
        set_state(_STATE_RALLY_SOLD_BTC, "0")
        logger.info("[HARVEST] Rally engaged — start stack %.8f BTC", btc_balance)


def _mark_threshold_crossed_if_new() -> None:
    """Record when the trend gate was first crossed, for the sustain timer."""
    if not get_state(_STATE_THRESHOLD_SINCE):
        set_state(_STATE_THRESHOLD_SINCE, _now().isoformat())


def evaluate(cfg: Config, current_price: float, ma200,
             btc_balance: float, avg_cost_basis: float,
             regime: str) -> dict | None:
    """
    Decide whether to fire one harvest slice this tick.

    Returns a sell action dict {"type":"sell","reason":"harvest_fire",
    "btc_amount":...} or None. Side effects: maintains the rally bookkeeping in
    bot_state (threshold-crossed timestamp, rally active flag, start stack, and
    cumulative sold) so the sustain and per-rally caps work across ticks.
    """
    if ma200 is None or ma200 <= 0:
        return None

    ratio = current_price / ma200
    exit_ratio = cfg.harvest_exit_pct
    # In a breakout the entry gate relaxes to the exit ratio (begin trimming
    # earlier in a violent up-move); otherwise the full threshold is required.
    entry_gate = exit_ratio if regime == REGIME_BREAKOUT_UP else cfg.harvest_threshold_pct

    # ── Exit: price fell back toward/below the 200MA → harvesting off, reset.
    if ratio < exit_ratio:
        _end_rally()
        return None

    # In the neutral band (between exit and the entry gate) we neither harvest nor
    # reset — we just wait. Clear any stale sustain timer so a fresh push to the
    # gate must re-arm the sustain window.
    if ratio < entry_gate:
        set_state(_STATE_THRESHOLD_SINCE, "")
        return None

    # ── Trend gate satisfied. Arm/confirm the sustain timer.
    _mark_threshold_crossed_if_new()
    if _days_since(_STATE_THRESHOLD_SINCE) < cfg.harvest_sustain_days:
        logger.debug("[HARVEST] gate met but not yet sustained %d days",
                     cfg.harvest_sustain_days)
        return None

    # ── Stack floor: never harvest a near-zero (rebuilding) stack.
    if btc_balance < cfg.harvest_min_stack_btc:
        logger.debug("[HARVEST] stack %.8f below floor %.8f — suppressed",
                     btc_balance, cfg.harvest_min_stack_btc)
        return None

    _begin_rally_if_new(btc_balance)

    # ── Per-rally cap: stop once cumulative sells reach the cap of the stack as
    #    it stood at rally start. This is the hard guarantee against a v1.x drain.
    start_stack = _float_state(_STATE_RALLY_START_BTC, btc_balance)
    already_sold = _float_state(_STATE_RALLY_SOLD_BTC, 0.0)
    rally_cap_btc = start_stack * cfg.harvest_total_cap_pct
    remaining_cap = rally_cap_btc - already_sold
    if remaining_cap <= 0:
        logger.info("[HARVEST] per-rally cap reached (%.8f BTC) — no more this rally",
                    rally_cap_btc)
        return None

    # ── Profit gate: only trim slices that are genuinely above cost basis.
    if avg_cost_basis > 0:
        profit_pct = (current_price - avg_cost_basis) / avg_cost_basis
        if profit_pct < cfg.harvest_tier_trigger_pct:
            logger.debug("[HARVEST] profit %.1f%% below tier trigger %.1f%%",
                         profit_pct * 100, cfg.harvest_tier_trigger_pct * 100)
            return None

    # ── Size this fire: per-fire cap, but never exceed the remaining rally cap.
    fire_btc = min(btc_balance * cfg.harvest_fire_cap_pct, remaining_cap)
    if fire_btc * current_price < cfg.min_order_usd:
        logger.debug("[HARVEST] slice $%.2f below min order — skipping",
                     fire_btc * current_price)
        return None

    set_state(_STATE_RALLY_SOLD_BTC, str(already_sold + fire_btc))
    logger.info("[HARVEST] firing: selling %.8f BTC @ $%.2f (rally sold %.8f / cap %.8f)",
                fire_btc, current_price, already_sold + fire_btc, rally_cap_btc)
    return {"type": "sell", "reason": "harvest_fire", "btc_amount": fire_btc}


def get_harvest_status(cfg: Config, current_price: float, ma200) -> dict:
    """Build the Harvest block for /api/status (additive, v2-only)."""
    active = get_state(_STATE_RALLY_ACTIVE, "false") == "true"
    start_stack = _float_state(_STATE_RALLY_START_BTC, 0.0)
    sold = _float_state(_STATE_RALLY_SOLD_BTC, 0.0)
    rally_cap_btc = start_stack * cfg.harvest_total_cap_pct
    ratio = (current_price / ma200) if (ma200 and ma200 > 0) else None
    return {
        "active":         active,
        "ma_ratio":       round(ratio, 4) if ratio is not None else None,
        "threshold_pct":  cfg.harvest_threshold_pct,
        "rally_sold_btc": round(sold, 8),
        "rally_cap_btc":  round(rally_cap_btc, 8),
        "cap_remaining_btc": round(max(0.0, rally_cap_btc - sold), 8),
    }
