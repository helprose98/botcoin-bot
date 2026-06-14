"""
config.py — Loads and validates all settings from the .env file.
Every configurable value lives here. No magic numbers anywhere else.
"""

import os
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


def _get(key: str, default=None, required=False):
    val = os.environ.get(key, default)
    if required and not val:
        raise EnvironmentError(f"Required config key missing: {key}")
    return val


def _float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        logger.warning("Invalid float for %s, using default %s", key, default)
        return default


def _int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, str(default)).lower()
    return val in ("true", "1", "yes")


@dataclass
class Config:
    # Kraken credentials
    api_key:    str
    api_secret: str

    # Trading pair
    trading_pair: str

    # DCA
    dca_amount_usd:  float
    dca_frequency:   str   # "daily", "weekly", or "monthly"
    dca_day:         str   # e.g. "monday" (weekly only)
    dca_day_of_month:int   # 1–28 (monthly only)
    dca_time_utc:    str   # e.g. "14:00"

    # Capital pools
    recycler_pool_percent: float  # 0.0–1.0

    # Dip buyer
    dip_threshold_pct:      float
    dip_buy_deploy_pct:     float
    dip_cooldown_hours:     int
    dip_tier2_threshold:    float
    dip_tier2_deploy:       float
    dip_tier3_threshold:    float
    dip_tier3_deploy:       float

    # Recycler (sell/rebuy)
    recycler_sell_threshold_pct: float
    recycler_sell_pct:           float
    recycler_sell_cooldown_hours:int
    recycler_rebuy_drop_pct:     float

    # Fees
    maker_fee: float

    # Safety
    min_usd_reserve: float
    min_order_usd:   float
    max_order_usd:   float

    # Accumulation mode
    mode: str          # 'btc_accumulate', 'usd_accumulate', or 'auto'

    # Sideways Market
    sideways_enabled:       bool
    range_trade_size_usd:   float
    range_max_positions:    int

    # Volatility-adaptive thresholds (Tier 1)
    volatility_adaptive_enabled: bool
    vol_multiplier_min:          float
    vol_multiplier_max:          float

    # Anti-thrash dampener (Tier 1)
    min_gap_between_trades_seconds: int
    max_trades_per_day:             int

    # ── Strategy v2.0 ──────────────────────────────────────────────────────────
    # Runtime strategy selector. "v1" runs the legacy BTC/USD accumulation stack
    # (unchanged); "v2" runs the regime-driven Universal Recycler + Harvest stack.
    # Defaults to "v1" so upgrading the code never silently changes trading
    # behavior — the operator opts into v2 explicitly.
    strategy_version: str

    # Harvest mode — the ONLY net stack-reduction path in v2. Gated on a confirmed
    # rally (price sustained above 200MA × threshold) and hard-capped per fire and
    # per rally so it can never repeat the v1.x stack drain.
    harvest_threshold_pct:    float  # price/200MA ratio that opens Harvest
    harvest_exit_pct:         float  # ratio below which Harvest turns off
    harvest_sustain_days:     int    # days above threshold before harvesting
    harvest_fire_cap_pct:     float  # max fraction of stack sold per fire
    harvest_total_cap_pct:    float  # max fraction of rally-start stack per rally
    harvest_tier_trigger_pct: float  # profit-above-basis needed to fire a slice
    harvest_min_stack_btc:    float  # suppress Harvest below this stack floor

    # Universal Recycler — always-on, volatility-adaptive range cycling.
    recycler_position_cap_usd: float  # hard per-position USD cap
    recycler_position_pct:     float  # per-position size as fraction of portfolio
    recycler_min_positions:    int    # floor on max concurrent open positions
    recycler_position_divisor: float  # portfolio_usd / this = max position count
    recycler_time_limit_days:  int    # open-position age before long-hold convert
    recycler_band_reference:   str    # band midpoint source

    # Regime detector — event-driven chop/breakout_up/breakdown/cooling.
    breakout_atr_multiplier:    float  # ATR ratio (+ N-day high break) → breakout_up
    breakdown_atr_multiplier:   float  # ATR ratio (+ N-day low break) → breakdown
    regime_break_lookback_days: int    # N-day high/low window for break detection
    regime_cooling_quiet_hours: int    # quiet hours before cooling → chop

    # Position-conversion / DCA shaping (v2).
    breakdown_max_hold_days: int    # max hold for underwater positions before convert
    dca_harvest_scale:       float  # DCA multiplier while in Harvest regime
    rebuild_dca_amount_usd:  float  # accelerated DCA amount during rebuild phase
    rebuild_dca_days:        int    # days of accelerated DCA before reverting

    # Logging
    log_level: str
    log_file:  str


# All known .env keys with their safe defaults.
# When new keys are added here, they get written to .env automatically on startup.
_ENV_DEFAULTS = {
    "TRADING_PAIR":                   "XBTUSD",
    "MODE":                           "auto",
    "DCA_AMOUNT_USD":                 "50.0",
    "DCA_FREQUENCY":                  "weekly",
    "DCA_DAY":                        "monday",
    "DCA_DAY_OF_MONTH":               "1",
    "DCA_TIME_UTC":                   "13:00",
    "RECYCLER_POOL_PERCENT":          "0.35",
    "DIP_THRESHOLD_PERCENT":          "0.07",
    "DIP_BUY_DEPLOY_PERCENT":         "0.60",
    "DIP_COOLDOWN_HOURS":             "12",
    "DIP_TIER2_THRESHOLD_PERCENT":    "0.15",
    "DIP_TIER2_DEPLOY_PERCENT":       "0.80",
    "DIP_TIER3_THRESHOLD_PERCENT":    "0.22",
    "DIP_TIER3_DEPLOY_PERCENT":       "1.00",
    "RECYCLER_SELL_THRESHOLD_PERCENT":"0.18",
    "RECYCLER_SELL_PERCENT":          "0.18",
    "RECYCLER_SELL_COOLDOWN_HOURS":   "36",
    "RECYCLER_REBUY_DROP_PERCENT":    "0.08",
    "KRAKEN_MAKER_FEE":               "0.0025",
    "MIN_USD_RESERVE":                "10.0",
    "MIN_ORDER_USD":                  "5.0",
    "MAX_ORDER_USD":                  "2000.0",
    "SIDEWAYS_ENABLED":               "true",
    "RANGE_TRADE_SIZE_USD":           "500",
    "RANGE_MAX_POSITIONS":            "5",
    "VOLATILITY_ADAPTIVE_ENABLED":    "true",
    "VOL_MULTIPLIER_MIN":             "0.7",
    "VOL_MULTIPLIER_MAX":             "1.5",
    "MIN_GAP_BETWEEN_TRADES_SECONDS": "3600",
    "MAX_TRADES_PER_DAY":             "8",
    # ── Strategy v2.0 (defaults from the locked-decisions table, 2026-06-13) ──
    # STRATEGY_VERSION ships "v1": existing bots keep their exact behavior after
    # an upgrade and only move to v2 when the operator flips this key.
    "STRATEGY_VERSION":               "v1",
    "HARVEST_THRESHOLD_PCT":          "1.15",
    "HARVEST_EXIT_PCT":               "1.05",
    "HARVEST_SUSTAIN_DAYS":           "3",
    "HARVEST_FIRE_CAP_PCT":           "0.02",
    "HARVEST_TOTAL_CAP_PCT":          "0.33",
    "HARVEST_TIER_TRIGGER_PCT":       "0.05",
    "HARVEST_MIN_STACK_BTC":          "0.02",
    "RECYCLER_POSITION_CAP_USD":      "500",
    "RECYCLER_POSITION_PCT":          "0.05",
    "RECYCLER_MIN_POSITIONS":         "5",
    "RECYCLER_POSITION_DIVISOR":      "1000",
    "RECYCLER_TIME_LIMIT_DAYS":       "90",
    "RECYCLER_BAND_REFERENCE":        "vwap_24h",
    "BREAKOUT_ATR_MULTIPLIER":        "2.0",
    "BREAKDOWN_ATR_MULTIPLIER":       "2.0",
    "REGIME_BREAK_LOOKBACK_DAYS":     "14",
    "REGIME_COOLING_QUIET_HOURS":     "8",
    "BREAKDOWN_MAX_HOLD_DAYS":        "30",
    "DCA_HARVEST_SCALE":              "0.5",
    "REBUILD_DCA_AMOUNT_USD":         "100",
    "REBUILD_DCA_DAYS":               "60",
    "LOG_LEVEL":                      "INFO",
    "LOG_FILE":                       "/app/data/bot.log",
}

# Accepted band-reference sources for the Universal Recycler (Q-new-1).
_RECYCLER_BAND_REFERENCES = ("vwap_24h", "mid_recent_hl", "last_close")

# Accepted strategy stacks.
_STRATEGY_VERSIONS = ("v1", "v2")


def _sync_env_defaults(env_path: str = "/app/.env"):
    """
    Write any missing keys to .env with safe defaults.
    Existing values are never overwritten — only missing keys are added.
    API keys and passwords are never touched.
    Called once on startup after credentials are confirmed present.
    """
    import pathlib
    path = pathlib.Path(env_path)
    if not path.exists():
        return
    try:
        existing_keys = set()
        lines = path.read_text().splitlines()
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                existing_keys.add(line.split("=", 1)[0].strip())

        missing = {k: v for k, v in _ENV_DEFAULTS.items() if k not in existing_keys}
        if missing:
            with path.open("a") as f:
                f.write("\n# Auto-added defaults (new in this version)\n")
                for k, v in missing.items():
                    f.write(f"{k}={v}\n")
                    os.environ.setdefault(k, v)
            logger.info("[config] Added %d new default keys to .env: %s",
                        len(missing), list(missing.keys()))
    except Exception as e:
        logger.warning("[config] Could not sync .env defaults: %s", e)


def load_config() -> Config:
    """Load config from environment variables (populated from .env by Docker)."""
    # Sync any missing keys to .env with safe defaults before loading
    _sync_env_defaults()

    cfg = Config(
        api_key    = _get("KRAKEN_API_KEY",    required=True),
        api_secret = _get("KRAKEN_API_SECRET", required=True),

        trading_pair = _get("TRADING_PAIR", "XBTUSD"),

        dca_amount_usd   = _float("DCA_AMOUNT_USD", 50.0),
        dca_frequency    = _get("DCA_FREQUENCY", "weekly").lower(),
        dca_day          = _get("DCA_DAY", "monday").lower(),
        dca_day_of_month = _int("DCA_DAY_OF_MONTH", 1),
        dca_time_utc     = _get("DCA_TIME_UTC", "14:00"),

        recycler_pool_percent = _float("RECYCLER_POOL_PERCENT", 0.20),

        dip_threshold_pct  = _float("DIP_THRESHOLD_PERCENT", 0.10),
        dip_buy_deploy_pct = _float("DIP_BUY_DEPLOY_PERCENT", 0.50),
        dip_cooldown_hours = _int("DIP_COOLDOWN_HOURS", 24),
        dip_tier2_threshold = _float("DIP_TIER2_THRESHOLD_PERCENT", 0.20),
        dip_tier2_deploy    = _float("DIP_TIER2_DEPLOY_PERCENT", 0.75),
        dip_tier3_threshold = _float("DIP_TIER3_THRESHOLD_PERCENT", 0.30),
        dip_tier3_deploy    = _float("DIP_TIER3_DEPLOY_PERCENT", 1.00),

        recycler_sell_threshold_pct  = _float("RECYCLER_SELL_THRESHOLD_PERCENT", 0.18),
        recycler_sell_pct            = _float("RECYCLER_SELL_PERCENT", 0.10),
        recycler_sell_cooldown_hours = _int("RECYCLER_SELL_COOLDOWN_HOURS", 72),
        recycler_rebuy_drop_pct      = _float("RECYCLER_REBUY_DROP_PERCENT", 0.15),

        maker_fee = _float("KRAKEN_MAKER_FEE", 0.0025),

        min_usd_reserve = _float("MIN_USD_RESERVE", 10.0),
        min_order_usd   = _float("MIN_ORDER_USD", 5.0),
        max_order_usd   = _float("MAX_ORDER_USD", 500.0),

        mode = _get("MODE", "auto").lower(),

        sideways_enabled     = _bool("SIDEWAYS_ENABLED", True),
        range_trade_size_usd = _float("RANGE_TRADE_SIZE_USD", 500.0),
        range_max_positions  = _int("RANGE_MAX_POSITIONS", 5),

        volatility_adaptive_enabled = _bool("VOLATILITY_ADAPTIVE_ENABLED", True),
        vol_multiplier_min          = _float("VOL_MULTIPLIER_MIN", 0.7),
        vol_multiplier_max          = _float("VOL_MULTIPLIER_MAX", 1.5),

        min_gap_between_trades_seconds = _int("MIN_GAP_BETWEEN_TRADES_SECONDS", 3600),
        max_trades_per_day             = _int("MAX_TRADES_PER_DAY", 8),

        strategy_version = _get("STRATEGY_VERSION", "v1").lower().strip(),

        harvest_threshold_pct    = _float("HARVEST_THRESHOLD_PCT", 1.15),
        harvest_exit_pct         = _float("HARVEST_EXIT_PCT", 1.05),
        harvest_sustain_days     = _int("HARVEST_SUSTAIN_DAYS", 3),
        harvest_fire_cap_pct     = _float("HARVEST_FIRE_CAP_PCT", 0.02),
        harvest_total_cap_pct    = _float("HARVEST_TOTAL_CAP_PCT", 0.33),
        harvest_tier_trigger_pct = _float("HARVEST_TIER_TRIGGER_PCT", 0.05),
        harvest_min_stack_btc    = _float("HARVEST_MIN_STACK_BTC", 0.02),

        recycler_position_cap_usd = _float("RECYCLER_POSITION_CAP_USD", 500.0),
        recycler_position_pct     = _float("RECYCLER_POSITION_PCT", 0.05),
        recycler_min_positions    = _int("RECYCLER_MIN_POSITIONS", 5),
        recycler_position_divisor = _float("RECYCLER_POSITION_DIVISOR", 1000.0),
        recycler_time_limit_days  = _int("RECYCLER_TIME_LIMIT_DAYS", 90),
        recycler_band_reference   = _get("RECYCLER_BAND_REFERENCE", "vwap_24h").lower().strip(),

        breakout_atr_multiplier    = _float("BREAKOUT_ATR_MULTIPLIER", 2.0),
        breakdown_atr_multiplier   = _float("BREAKDOWN_ATR_MULTIPLIER", 2.0),
        regime_break_lookback_days = _int("REGIME_BREAK_LOOKBACK_DAYS", 14),
        regime_cooling_quiet_hours = _int("REGIME_COOLING_QUIET_HOURS", 8),

        breakdown_max_hold_days = _int("BREAKDOWN_MAX_HOLD_DAYS", 30),
        dca_harvest_scale       = _float("DCA_HARVEST_SCALE", 0.5),
        rebuild_dca_amount_usd  = _float("REBUILD_DCA_AMOUNT_USD", 100.0),
        rebuild_dca_days        = _int("REBUILD_DCA_DAYS", 60),

        log_level = _get("LOG_LEVEL", "INFO"),
        log_file  = _get("LOG_FILE", "/app/logs/bot.log"),
    )

    # Validate
    if not 0 < cfg.recycler_pool_percent < 1:
        raise ValueError("RECYCLER_POOL_PERCENT must be between 0 and 1")
    if cfg.dca_frequency not in ("daily", "weekly", "monthly"):
        raise ValueError(f"Invalid DCA_FREQUENCY: {cfg.dca_frequency}. Must be daily, weekly, or monthly")
    if cfg.dca_frequency == "weekly" and cfg.dca_day not in (
            "monday","tuesday","wednesday","thursday","friday","saturday","sunday"):
        raise ValueError(f"Invalid DCA_DAY: {cfg.dca_day}")
    if cfg.dca_frequency == "monthly" and not (1 <= cfg.dca_day_of_month <= 28):
        raise ValueError(f"DCA_DAY_OF_MONTH must be 1–28, got: {cfg.dca_day_of_month}")
    if cfg.mode not in ("btc_accumulate", "usd_accumulate", "auto"):
        raise ValueError(f"Invalid MODE: {cfg.mode}. Must be btc_accumulate, usd_accumulate, or auto")
    if not 0 < cfg.vol_multiplier_min <= 1.0 <= cfg.vol_multiplier_max:
        raise ValueError("VOL_MULTIPLIER_MIN must be in (0,1] and VOL_MULTIPLIER_MAX must be >= 1.0")
    if cfg.min_gap_between_trades_seconds < 0:
        raise ValueError("MIN_GAP_BETWEEN_TRADES_SECONDS must be >= 0")
    if cfg.max_trades_per_day < 1:
        raise ValueError("MAX_TRADES_PER_DAY must be >= 1")

    # ── Strategy v2.0 validation (spec §5) ─────────────────────────────────────
    if cfg.strategy_version not in _STRATEGY_VERSIONS:
        raise ValueError(
            f"Invalid STRATEGY_VERSION: {cfg.strategy_version}. "
            f"Must be one of {_STRATEGY_VERSIONS}")
    # Harvest gate must sit strictly above the exit, and both strictly above the
    # 200MA (ratio > 1.0), or Harvest would fire at or below trend.
    if not 1.0 < cfg.harvest_exit_pct < cfg.harvest_threshold_pct:
        raise ValueError(
            "HARVEST_EXIT_PCT must satisfy 1.0 < exit < HARVEST_THRESHOLD_PCT")
    # Per-fire cap can never exceed the per-rally cap, and neither can exceed the
    # whole stack — this is the structural guarantee against another -99% drain.
    if not 0 < cfg.harvest_fire_cap_pct <= cfg.harvest_total_cap_pct <= 1:
        raise ValueError(
            "Require 0 < HARVEST_FIRE_CAP_PCT <= HARVEST_TOTAL_CAP_PCT <= 1")
    if cfg.harvest_tier_trigger_pct < 0:
        raise ValueError("HARVEST_TIER_TRIGGER_PCT must be >= 0")
    if cfg.harvest_sustain_days < 0:
        raise ValueError("HARVEST_SUSTAIN_DAYS must be >= 0")
    if cfg.harvest_min_stack_btc < 0:
        raise ValueError("HARVEST_MIN_STACK_BTC must be >= 0")
    if cfg.breakout_atr_multiplier <= 1.0 or cfg.breakdown_atr_multiplier <= 1.0:
        raise ValueError(
            "BREAKOUT/BREAKDOWN_ATR_MULTIPLIER must be > 1.0 (a multiple of baseline ATR)")
    if cfg.regime_break_lookback_days <= 0:
        raise ValueError("REGIME_BREAK_LOOKBACK_DAYS must be > 0")
    if cfg.regime_cooling_quiet_hours <= 0:
        raise ValueError("REGIME_COOLING_QUIET_HOURS must be > 0")
    if cfg.recycler_time_limit_days <= 0:
        raise ValueError("RECYCLER_TIME_LIMIT_DAYS must be > 0")
    if cfg.breakdown_max_hold_days <= 0:
        raise ValueError("BREAKDOWN_MAX_HOLD_DAYS must be > 0")
    if not 0 < cfg.recycler_position_pct < 1:
        raise ValueError("RECYCLER_POSITION_PCT must be between 0 and 1")
    if cfg.recycler_position_cap_usd <= 0:
        raise ValueError("RECYCLER_POSITION_CAP_USD must be > 0")
    if cfg.recycler_position_divisor <= 0:
        raise ValueError("RECYCLER_POSITION_DIVISOR must be > 0")
    if cfg.recycler_min_positions < 1:
        raise ValueError("RECYCLER_MIN_POSITIONS must be >= 1")
    if cfg.recycler_band_reference not in _RECYCLER_BAND_REFERENCES:
        raise ValueError(
            f"Invalid RECYCLER_BAND_REFERENCE: {cfg.recycler_band_reference}. "
            f"Must be one of {_RECYCLER_BAND_REFERENCES}")
    if cfg.dca_harvest_scale < 0:
        raise ValueError("DCA_HARVEST_SCALE must be >= 0")
    if cfg.rebuild_dca_amount_usd < 0:
        raise ValueError("REBUILD_DCA_AMOUNT_USD must be >= 0")
    if cfg.rebuild_dca_days < 0:
        raise ValueError("REBUILD_DCA_DAYS must be >= 0")

    logger.info("Config loaded — DCA: $%.2f/%s @ %s UTC | strategy=%s",
                cfg.dca_amount_usd, cfg.dca_frequency, cfg.dca_time_utc,
                cfg.strategy_version)
    return cfg
