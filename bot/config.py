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

    # Mode
    paper_trading: bool

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
    "PAPER_TRADING":                  "true",
    "LOG_LEVEL":                      "INFO",
    "LOG_FILE":                       "/app/data/bot.log",
}


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

        paper_trading = _bool("PAPER_TRADING", True),

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

    mode = "PAPER TRADING" if cfg.paper_trading else "LIVE TRADING"
    logger.info("Config loaded — Mode: %s | DCA: $%.2f/%s @ %s UTC",
                mode, cfg.dca_amount_usd, cfg.dca_frequency, cfg.dca_time_utc)
    return cfg
