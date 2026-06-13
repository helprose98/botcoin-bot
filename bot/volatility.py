"""
volatility.py — Volatility metrics for adaptive trading thresholds.

The bot's dip/spike thresholds are calibrated for a "normal" market. When
realized volatility deviates from baseline, the thresholds should adapt:
  - Low vol (calm):  tighten thresholds → catch smaller dips/spikes that are
    proportionally meaningful in a quiet market.
  - High vol (storm): loosen thresholds → require larger moves before
    triggering, to filter noise and avoid catching falling knives.

ATR (Average True Range) over a 14-day window is the standard measure. We compare
the recent 14-day ATR% against a rolling 90-day baseline (median of 14-day ATR%
windows) and clamp the ratio into a configured multiplier band.

Everything here degrades gracefully: any failure to obtain or compute OHLC returns
a multiplier of 1.0 so trading proceeds against the unadjusted base thresholds.
Volatility moves slowly, so the result is cached for an hour to avoid hammering
Kraken's public OHLC endpoint.
"""

from __future__ import annotations

import time
import logging
from typing import Tuple

logger = logging.getLogger(__name__)

# How often to recompute ATR. Volatility moves slowly; a 1h cache is plenty.
ATR_CACHE_TTL_SECONDS = 3600

# Default bounds on the multiplier applied to base thresholds. These are the
# fallback values; callers pass the config-driven bounds into calculate_atr.
# Below 1.0 = tighten (calmer than baseline). Above 1.0 = loosen.
DEFAULT_VOL_MULTIPLIER_MIN = 0.7
DEFAULT_VOL_MULTIPLIER_MAX = 1.5

# Daily OHLC window used for the recent ATR.
ATR_PERIOD_DAYS = 14

# Days of rolling 14-day ATR% windows used to form the baseline median.
ATR_BASELINE_DAYS = 90

# Kraken OHLC interval (minutes) for daily candles.
_DAILY_INTERVAL_MINUTES = 1440

# Module-level cache. `multiplier` is stored alongside the raw metrics so a cache
# hit can be returned without re-clamping.
_cache: dict = {"ts": 0.0, "atr_pct": None, "baseline_pct": None, "multiplier": 1.0}


def _clamp_multiplier(atr_pct, baseline_pct, mult_min: float, mult_max: float) -> float:
    """Clamp atr_pct / baseline_pct into the configured multiplier band."""
    if atr_pct is None or baseline_pct is None or baseline_pct <= 0:
        return 1.0
    raw = atr_pct / baseline_pct
    return max(mult_min, min(mult_max, raw))


def calculate_atr(client,
                  mult_min: float = DEFAULT_VOL_MULTIPLIER_MIN,
                  mult_max: float = DEFAULT_VOL_MULTIPLIER_MAX
                  ) -> Tuple[float | None, float | None, float]:
    """
    Compute (atr_pct, baseline_atr_pct, vol_multiplier) from Kraken's daily
    OHLC for the trading pair.

    Returns (None, None, 1.0) on any failure or insufficient data so the caller
    falls back to base thresholds — volatility computation never blocks trading.
    Results are cached for ATR_CACHE_TTL_SECONDS.
    """
    now = time.time()
    if now - _cache["ts"] < ATR_CACHE_TTL_SECONDS and _cache["atr_pct"] is not None:
        return _cache["atr_pct"], _cache["baseline_pct"], _cache["multiplier"]

    try:
        # Daily candles give true high/low/close for a real ATR.
        candles = client.get_ohlc(interval_minutes=_DAILY_INTERVAL_MINUTES)
        # candles: [[time, open, high, low, close, vwap, volume, count], ...]
        if not candles or len(candles) < ATR_BASELINE_DAYS + 1:
            logger.warning("[atr] insufficient OHLC data: %d candles",
                           len(candles or []))
            return None, None, 1.0

        # True Range per candle (skip the first — it needs a previous close).
        # Store the close alongside each TR for percentage normalization.
        trs = []
        for i in range(1, len(candles)):
            high = float(candles[i][2])
            low = float(candles[i][3])
            close = float(candles[i][4])
            prev_close = float(candles[i - 1][4])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append((close, tr))

        # Recent ATR (last 14 days) as a fraction of the current close.
        recent = trs[-ATR_PERIOD_DAYS:]
        atr_recent = sum(tr for _, tr in recent) / len(recent)
        current_close = recent[-1][0]
        if current_close <= 0:
            return None, None, 1.0
        atr_pct = atr_recent / current_close

        # Baseline = median of rolling 14-day ATR% windows over the history.
        rolling_pcts = []
        for end in range(ATR_PERIOD_DAYS, len(trs)):
            window = trs[end - ATR_PERIOD_DAYS:end]
            window_atr = sum(tr for _, tr in window) / len(window)
            window_close = window[-1][0]
            if window_close > 0:
                rolling_pcts.append(window_atr / window_close)
        if not rolling_pcts:
            return None, None, 1.0
        rolling_pcts.sort()
        baseline_pct = rolling_pcts[len(rolling_pcts) // 2]

        multiplier = _clamp_multiplier(atr_pct, baseline_pct, mult_min, mult_max)
        _cache.update({"ts": now, "atr_pct": atr_pct,
                       "baseline_pct": baseline_pct, "multiplier": multiplier})
        logger.info("[atr] recomputed: atr_pct=%.4f baseline=%.4f multiplier=%.3f",
                    atr_pct, baseline_pct, multiplier)
        return atr_pct, baseline_pct, multiplier

    except Exception as e:
        logger.warning("[atr] computation failed, using flat thresholds: %s", e)
        return None, None, 1.0


def apply_multiplier(base_threshold: float, multiplier: float) -> float:
    """
    Apply the vol multiplier to a base threshold (e.g. dip_threshold_pct) and
    return the effective threshold the strategy should compare against.
    """
    return base_threshold * multiplier
