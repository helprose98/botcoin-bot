"""
regime_detector.py — Event-driven market-regime state machine (Strategy v2.0).

Regimes
───────
  chop         Default. Normal volatility, no fresh N-day price break. The
               Universal Recycler runs its normal bands here.
  breakout_up  Price broke the N-day high AND realized volatility spiked above
               BREAKOUT_ATR_MULTIPLIER × baseline. Pause opening new recycler
               positions (don't chase); let existing positions close into
               strength; Harvest's gate is temporarily relaxed.
  breakdown    Price broke the N-day low AND volatility spiked. Pause new buys
               and — critically — HOLD existing positions (no panic selling into
               a flush). This is the explicit fix for the v1.x behavior that
               sold into downtrends and drained the stack.
  cooling      Volatility has calmed and there is no fresh break, but we wait
               REGIME_COOLING_QUIET_HOURS before declaring normal again, to
               avoid flip-flopping on a single quiet tick.

Event model
───────────
The bot polls every 300s, but regime *transitions* are treated as first-class
events: `evaluate()` returns a non-None `transition` dict EXACTLY ONCE, at the
boundary tick where conditions flip. There is NO multi-check confirmation gate
on entering breakout_up/breakdown (unlike the v1 Sideways overlay's 2-check
hysteresis) — a single tick that sees the break + ATR spike flips the regime,
because hockey-stick moves are exactly what must not be missed. The only
intentional delay is the cooling timer on the way back down to chop.

The current regime is persisted in bot_state so a restart resumes where it left
off; every transition is appended to the regime_transitions ledger for audit.

Prime directive: keep the bot from buying into knives or panic-selling flushes,
so the stack survives to compound into more BTC over the long run.
"""

import logging
from datetime import datetime, timezone

from config import Config
from database import (
    get_state, set_state, get_recent_high, get_recent_low,
    record_regime_transition,
)

logger = logging.getLogger(__name__)


# Regime identifiers. Kept as plain strings (not an Enum) to match the codebase's
# bot_state value convention (everything in bot_state is stored as text).
REGIME_CHOP        = "chop"
REGIME_BREAKOUT_UP = "breakout_up"
REGIME_BREAKDOWN   = "breakdown"
REGIME_COOLING     = "cooling"

ALL_REGIMES = (REGIME_CHOP, REGIME_BREAKOUT_UP, REGIME_BREAKDOWN, REGIME_COOLING)

# When already in an active (breakout/breakdown) regime, volatility must fall back
# to within this multiple of baseline before we begin cooling. Sits below the
# entry multipliers so there is a clear band between "ignite" and "calm".
COOLING_ATR_CEILING = 1.6

# bot_state keys owned by this module.
_STATE_REGIME            = "regime_current"
_STATE_REGIME_ENTERED    = "regime_entered_ts"
_STATE_LAST_HIGH_BREAK   = "regime_last_high_break_ts"
_STATE_LAST_LOW_BREAK    = "regime_last_low_break_ts"


def _now() -> datetime:
    """Current UTC time (single source so tests can reason about it)."""
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def get_current_regime() -> str:
    """Return the persisted current regime, defaulting to chop on a cold start."""
    regime = get_state(_STATE_REGIME, REGIME_CHOP)
    return regime if regime in ALL_REGIMES else REGIME_CHOP


def _atr_ratio(atr_pct, baseline_pct) -> float:
    """
    Raw (unclamped) ATR-vs-baseline ratio.

    Deliberately uses the RAW ratio, NOT the clamped vol_multiplier the dip/spike
    thresholds use (Q-new-4 in the impl spec): the clamp band (e.g. 0.7–1.5) would
    make a ">2× baseline" breakout impossible to ever detect. Degrades to 1.0
    (normal) when volatility data is unavailable.
    """
    if atr_pct is None or baseline_pct is None or baseline_pct <= 0:
        return 1.0
    return atr_pct / baseline_pct


def _hours_since(state_key: str) -> float:
    """Hours since the timestamp stored under state_key, or +inf if never set."""
    ts = get_state(state_key)
    if not ts:
        return float("inf")
    try:
        last = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return float("inf")
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (_now() - last).total_seconds() / 3600.0


def _record_breaks(broke_high: bool, broke_low: bool) -> None:
    """Persist the timestamp of any fresh break, for the cooling-quiet timer."""
    if broke_high:
        set_state(_STATE_LAST_HIGH_BREAK, _now_iso())
    if broke_low:
        set_state(_STATE_LAST_LOW_BREAK, _now_iso())


def evaluate(cfg: Config, current_price: float,
             atr_pct, baseline_pct, vol_multiplier: float):
    """
    Decide the regime for this tick.

    Reads the persisted current regime and live signals (N-day high/low break +
    raw ATR ratio) and returns:
        (new_regime: str, transition: dict | None)

    `transition` is non-None ONLY when the regime actually changes — that single
    return is the "event". The caller logs/commits it and steers the same tick's
    dispatch by `new_regime`. This function does NOT itself persist anything
    except the break timestamps it needs for the cooling timer; the caller calls
    commit_transition() so persistence and the audit row stay together.
    """
    prev = get_current_regime()
    ratio = _atr_ratio(atr_pct, baseline_pct)

    lookback_hours = cfg.regime_break_lookback_days * 24
    high_n = get_recent_high(hours=lookback_hours)
    low_n  = get_recent_low(hours=lookback_hours)
    broke_high = bool(high_n) and current_price >= high_n
    broke_low  = bool(low_n) and current_price <= low_n
    _record_breaks(broke_high, broke_low)

    new = _next_regime(cfg, prev, ratio, broke_high, broke_low)

    if new == prev:
        return prev, None

    transition = {
        "from_regime":    prev,
        "to_regime":      new,
        "price_usd":      current_price,
        "atr_ratio":      round(ratio, 4),
        "vol_multiplier": round(vol_multiplier, 4),
    }
    return new, transition


def _next_regime(cfg: Config, prev: str, ratio: float,
                 broke_high: bool, broke_low: bool) -> str:
    """
    Pure transition function — given the previous regime and this tick's signals,
    return the regime for this tick. Split out from evaluate() so it is trivially
    unit-testable with no database.

    Priority: a fresh, volatile break always wins (so a breakdown can interrupt a
    cooling-from-breakout, and vice versa). Otherwise an active regime relaxes to
    cooling when volatility calms, and cooling resolves to chop after a quiet
    period.
    """
    igniting_up   = broke_high and ratio > cfg.breakout_atr_multiplier
    igniting_down = broke_low and ratio > cfg.breakdown_atr_multiplier

    # A fresh volatile break re-ignites from any state (highest priority).
    if igniting_up:
        return REGIME_BREAKOUT_UP
    if igniting_down:
        return REGIME_BREAKDOWN

    if prev in (REGIME_BREAKOUT_UP, REGIME_BREAKDOWN):
        # Leave an active regime only once volatility has calmed and there is no
        # fresh break this tick. We go to cooling, not straight to chop.
        if ratio <= COOLING_ATR_CEILING and not broke_high and not broke_low:
            return REGIME_COOLING
        return prev

    if prev == REGIME_COOLING:
        # Resume normal once quiet (no new break) for the configured window.
        quiet_hours = min(_hours_since(_STATE_LAST_HIGH_BREAK),
                          _hours_since(_STATE_LAST_LOW_BREAK))
        if quiet_hours >= cfg.regime_cooling_quiet_hours:
            return REGIME_CHOP
        return REGIME_COOLING

    # prev == chop and no ignition → stay in chop.
    return REGIME_CHOP


def commit_transition(transition: dict) -> None:
    """
    Persist a regime change: update bot_state and append the audit-ledger row in
    one call, so the live regime and its audit trail can never diverge.
    """
    set_state(_STATE_REGIME, transition["to_regime"])
    set_state(_STATE_REGIME_ENTERED, _now_iso())
    record_regime_transition(
        from_regime    = transition["from_regime"],
        to_regime      = transition["to_regime"],
        price_usd      = transition["price_usd"],
        atr_ratio      = transition.get("atr_ratio"),
        vol_multiplier = transition.get("vol_multiplier"),
    )


def get_regime_status() -> dict:
    """Return the current regime + when it was entered, for /api/status."""
    return {
        "state":      get_current_regime(),
        "entered_at": get_state(_STATE_REGIME_ENTERED),
    }
