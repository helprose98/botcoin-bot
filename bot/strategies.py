"""
strategies.py — All trading strategy logic for both accumulation modes.

BTC_ACCUMULATE mode  (bull market / stacking sats)
─────────────────────────────────────────────────
  • DCA:            Buy BTC on a schedule with USD
  • Dip buy:        Deploy USD reserve when price drops (3 tiers)
  • Recycler sell:  Sell small % of BTC when significantly above cost basis
  • Recycler rebuy: Rebuy that BTC when price corrects — net more BTC

USD_ACCUMULATE mode  (bear market / stacking dollars)
──────────────────────────────────────────────────────
  • DCA:            Sell BTC on a schedule for USD  (inverted)
  • Spike sell:     Deploy BTC reserve when price spikes (3 tiers, inverted)
  • Recycler buy:   Buy small % of USD worth of BTC when significantly below sell price
  • Recycler resell:Resell that BTC when price bounces — net more USD

The logic is perfectly symmetric. What BTC mode does with USD, USD mode does
with BTC — every parameter has a mirror counterpart.
"""

import logging
from datetime import datetime, timezone, timedelta

from config import Config
from database import (
    get_last_trade_by_reason, get_recent_high, get_recent_low,
    get_latest_snapshot, get_state, set_state
)

logger = logging.getLogger(__name__)


# ── Shared helpers ────────────────────────────────────────────────────────────

def _hours_since_last(reason: str) -> float:
    """Return hours elapsed since the last trade with the given reason."""
    trade = get_last_trade_by_reason(reason)
    if not trade:
        return float("inf")
    last_ts = datetime.fromisoformat(trade["timestamp"]).replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last_ts).total_seconds() / 3600


def _round_trip_fee_cost(amount_usd: float, maker_fee: float) -> float:
    """Total USD cost of a buy + sell round trip."""
    return amount_usd * maker_fee * 2


def _is_dca_due(reason: str, cfg: Config) -> bool:
    """
    Return True if a DCA trade is due based on the configured frequency.
    - daily:   hasn't fired in the last 23h
    - weekly:  hasn't fired in the last 6 days
    - monthly: hasn't fired in the last 27 days
    """
    min_hours = {"daily": 23, "weekly": 6 * 24, "monthly": 27 * 24}.get(cfg.dca_frequency, 6 * 24)
    trade = get_last_trade_by_reason(reason)
    if not trade:
        return True
    last_ts = datetime.fromisoformat(trade["timestamp"]).replace(tzinfo=timezone.utc)
    hours_elapsed = (datetime.now(timezone.utc) - last_ts).total_seconds() / 3600
    return hours_elapsed >= min_hours


def _in_dca_window(cfg: Config) -> bool:
    """
    Return True if the current time falls within the DCA execution window.
    - daily:   every day at dca_time_utc (30-min window)
    - weekly:  on dca_day at dca_time_utc (30-min window)
    - monthly: on dca_day_of_month at dca_time_utc (30-min window)
    """
    now_utc = datetime.now(timezone.utc)
    hour, minute = map(int, cfg.dca_time_utc.split(":"))
    scheduled = now_utc.replace(hour=hour, minute=minute, second=0, microsecond=0)
    in_time_window = (scheduled - timedelta(minutes=5)) <= now_utc <= (scheduled + timedelta(minutes=25))
    if not in_time_window:
        return False

    if cfg.dca_frequency == "daily":
        return True
    elif cfg.dca_frequency == "weekly":
        day_names = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]
        return day_names[now_utc.weekday()] == cfg.dca_day
    elif cfg.dca_frequency == "monthly":
        return now_utc.day == cfg.dca_day_of_month
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  BTC ACCUMULATE MODE
# ══════════════════════════════════════════════════════════════════════════════

def btc_check_dca(cfg: Config, usd_balance: float) -> dict | None:
    """
    BTC mode: Should we do the scheduled DCA buy this week?
    Returns action dict or None.
    """
    if not _in_dca_window(cfg):
        return None
    if not _is_dca_due("dca", cfg):
        logger.debug("BTC DCA: already executed this %s", cfg.dca_frequency)
        return None

    recycler_reserve = usd_balance * cfg.recycler_pool_percent
    dca_pool         = usd_balance - recycler_reserve - cfg.min_usd_reserve
    amount           = max(cfg.min_order_usd,
                           min(cfg.dca_amount_usd, dca_pool, cfg.max_order_usd))

    if amount < cfg.min_order_usd:
        logger.warning("BTC DCA: insufficient funds. DCA pool: $%.2f", dca_pool)
        return None

    logger.info("BTC DCA triggered: $%.2f", amount)
    return {"type": "buy", "usd_amount": amount, "reason": "dca"}


def btc_check_dip_buy(cfg: Config, current_price: float,
                      usd_balance: float) -> dict | None:
    """
    BTC mode: Deploy USD reserve when price drops from recent high.
    Three tiers — deeper dip = more capital deployed.
    """
    recent_high = get_recent_high(hours=168)
    if not recent_high:
        return None

    drop_pct = (recent_high - current_price) / recent_high
    logger.debug("BTC dip check: price=$%.2f high=$%.2f drop=%.1f%%",
                 current_price, recent_high, drop_pct * 100)

    # Determine tier
    if drop_pct >= cfg.dip_tier3_threshold:
        tier, deploy_pct, reason = 3, cfg.dip_tier3_deploy, "dip_buy_tier3"
    elif drop_pct >= cfg.dip_tier2_threshold:
        tier, deploy_pct, reason = 2, cfg.dip_tier2_deploy, "dip_buy_tier2"
    elif drop_pct >= cfg.dip_threshold_pct:
        tier, deploy_pct, reason = 1, cfg.dip_buy_deploy_pct, "dip_buy_tier1"
    else:
        return None

    if _hours_since_last("dip_buy_tier1") < cfg.dip_cooldown_hours or \
       _hours_since_last("dip_buy_tier2") < cfg.dip_cooldown_hours or \
       _hours_since_last("dip_buy_tier3") < cfg.dip_cooldown_hours:
        logger.debug("BTC dip tier %d: cooldown active", tier)
        return None

    recycler_pool = usd_balance * cfg.recycler_pool_percent
    usd_to_deploy = max(cfg.min_order_usd,
                        min(recycler_pool * deploy_pct,
                            cfg.max_order_usd,
                            usd_balance - cfg.min_usd_reserve))

    if usd_to_deploy < cfg.min_order_usd:
        logger.warning("BTC dip buy: recycler pool too small ($%.2f)", recycler_pool)
        return None

    logger.info("BTC dip buy tier %d: drop=%.1f%% deploying=$%.2f",
                tier, drop_pct * 100, usd_to_deploy)
    return {"type": "buy", "usd_amount": usd_to_deploy, "reason": reason,
            "tier": tier, "drop_pct": drop_pct}


def btc_check_recycler_sell(cfg: Config, current_price: float,
                             btc_balance: float,
                             avg_cost_basis: float) -> dict | None:
    """
    BTC mode: Sell a small slice of BTC when price is well above cost basis.
    Converts BTC → USD to reload the dip-buying reserve.
    """
    if avg_cost_basis <= 0 or btc_balance <= 0:
        return None

    profit_pct = (current_price - avg_cost_basis) / avg_cost_basis
    if profit_pct < cfg.recycler_sell_threshold_pct:
        logger.debug("BTC recycler sell: %.1f%% above basis (need %.1f%%)",
                     profit_pct * 100, cfg.recycler_sell_threshold_pct * 100)
        return None

    if _hours_since_last("recycler_sell") < cfg.recycler_sell_cooldown_hours:
        logger.debug("BTC recycler sell: cooldown active")
        return None

    btc_to_sell  = btc_balance * cfg.recycler_sell_pct
    usd_proceeds = btc_to_sell * current_price
    if usd_proceeds < _round_trip_fee_cost(usd_proceeds, cfg.maker_fee) * 3:
        logger.warning("BTC recycler sell: proceeds $%.2f don't justify fees", usd_proceeds)
        return None

    logger.info("BTC recycler SELL: %.1f%% above basis → selling %.8f BTC ($%.2f)",
                profit_pct * 100, btc_to_sell, usd_proceeds)
    set_state("btc_recycler_last_sell_price", str(current_price))
    set_state("btc_recycler_waiting_rebuy", "true")
    return {"type": "sell", "btc_amount": btc_to_sell, "reason": "recycler_sell",
            "profit_pct": profit_pct}


def btc_check_recycler_rebuy(cfg: Config, current_price: float,
                              usd_balance: float) -> dict | None:
    """
    BTC mode: After a recycler sell, rebuy when price has corrected.
    """
    if get_state("btc_recycler_waiting_rebuy", "false") != "true":
        return None

    sell_price = float(get_state("btc_recycler_last_sell_price", "0") or 0)
    if sell_price <= 0:
        return None

    drop_from_sell = (sell_price - current_price) / sell_price
    if drop_from_sell < cfg.recycler_rebuy_drop_pct:
        logger.debug("BTC recycler rebuy: waiting for %.1f%% drop from $%.2f (currently %.1f%%)",
                     cfg.recycler_rebuy_drop_pct * 100, sell_price, drop_from_sell * 100)
        return None

    recycler_pool = usd_balance * cfg.recycler_pool_percent
    usd_to_spend  = max(cfg.min_order_usd, min(recycler_pool, cfg.max_order_usd))
    if usd_to_spend < cfg.min_order_usd:
        return None

    logger.info("BTC recycler REBUY: price dropped %.1f%% from sell $%.2f → buying $%.2f",
                drop_from_sell * 100, sell_price, usd_to_spend)
    set_state("btc_recycler_waiting_rebuy", "false")
    return {"type": "buy", "usd_amount": usd_to_spend, "reason": "recycler_rebuy",
            "drop_pct": drop_from_sell}


# ══════════════════════════════════════════════════════════════════════════════
#  USD ACCUMULATE MODE  (perfectly inverted)
# ══════════════════════════════════════════════════════════════════════════════

def usd_check_dca(cfg: Config, current_price: float,
                  btc_balance: float) -> dict | None:
    """
    USD mode: Sell a fixed USD-equivalent of BTC each week on schedule.
    Mirror of btc_check_dca — sells BTC instead of buying it.
    """
    if not _in_dca_window(cfg):
        return None
    if not _is_dca_due("usd_dca_sell", cfg):
        logger.debug("USD DCA: already executed this %s", cfg.dca_frequency)
        return None

    # How much BTC to sell to get cfg.dca_amount_usd worth
    btc_reserve     = btc_balance * cfg.recycler_pool_percent
    available_btc   = btc_balance - btc_reserve
    target_btc      = cfg.dca_amount_usd / current_price

    btc_to_sell = min(target_btc, available_btc)
    min_btc     = cfg.min_order_usd / current_price
    max_btc     = cfg.max_order_usd / current_price

    btc_to_sell = max(min_btc, min(btc_to_sell, max_btc))

    if btc_to_sell < min_btc:
        logger.warning("USD DCA: insufficient BTC. Available: %.8f BTC", available_btc)
        return None

    logger.info("USD DCA triggered: sell %.8f BTC (~$%.2f)", btc_to_sell,
                btc_to_sell * current_price)
    return {"type": "sell", "btc_amount": btc_to_sell, "reason": "usd_dca_sell"}


def usd_check_spike_sell(cfg: Config, current_price: float,
                          btc_balance: float) -> dict | None:
    """
    USD mode: Deploy BTC reserve when price SPIKES above recent low.
    Mirror of btc_check_dip_buy — sells BTC on pumps instead of buying on dips.
    """
    recent_low = get_recent_low(hours=168)
    if not recent_low or recent_low <= 0:
        return None

    rise_pct = (current_price - recent_low) / recent_low
    logger.debug("USD spike check: price=$%.2f low=$%.2f rise=%.1f%%",
                 current_price, recent_low, rise_pct * 100)

    if rise_pct >= cfg.dip_tier3_threshold:
        tier, deploy_pct, reason = 3, cfg.dip_tier3_deploy, "usd_spike_sell_tier3"
    elif rise_pct >= cfg.dip_tier2_threshold:
        tier, deploy_pct, reason = 2, cfg.dip_tier2_deploy, "usd_spike_sell_tier2"
    elif rise_pct >= cfg.dip_threshold_pct:
        tier, deploy_pct, reason = 1, cfg.dip_buy_deploy_pct, "usd_spike_sell_tier1"
    else:
        return None

    if _hours_since_last("usd_spike_sell_tier1") < cfg.dip_cooldown_hours or \
       _hours_since_last("usd_spike_sell_tier2") < cfg.dip_cooldown_hours or \
       _hours_since_last("usd_spike_sell_tier3") < cfg.dip_cooldown_hours:
        logger.debug("USD spike tier %d: cooldown active", tier)
        return None

    btc_reserve  = btc_balance * cfg.recycler_pool_percent
    btc_to_sell  = btc_reserve * deploy_pct
    min_btc      = cfg.min_order_usd / current_price
    max_btc      = cfg.max_order_usd / current_price
    btc_to_sell  = max(min_btc, min(btc_to_sell, max_btc))

    if btc_to_sell < min_btc:
        logger.warning("USD spike sell: BTC reserve too small (%.8f BTC)", btc_reserve)
        return None

    logger.info("USD spike sell tier %d: rise=%.1f%% selling %.8f BTC ($%.2f)",
                tier, rise_pct * 100, btc_to_sell, btc_to_sell * current_price)
    return {"type": "sell", "btc_amount": btc_to_sell, "reason": reason,
            "tier": tier, "rise_pct": rise_pct}


def usd_check_recycler_buy(cfg: Config, current_price: float,
                            usd_balance: float,
                            avg_sell_basis: float) -> dict | None:
    """
    USD mode: Buy a small slice of BTC when price is well below our avg sell price.
    Mirror of btc_check_recycler_sell — accumulates BTC temporarily to resell higher.
    avg_sell_basis: the average price at which we've been selling BTC for USD.
    """
    if avg_sell_basis <= 0 or usd_balance <= 0:
        return None

    discount_pct = (avg_sell_basis - current_price) / avg_sell_basis
    if discount_pct < cfg.recycler_sell_threshold_pct:
        logger.debug("USD recycler buy: price only %.1f%% below sell basis $%.2f (need %.1f%%)",
                     discount_pct * 100, avg_sell_basis, cfg.recycler_sell_threshold_pct * 100)
        return None

    if _hours_since_last("usd_recycler_buy") < cfg.recycler_sell_cooldown_hours:
        logger.debug("USD recycler buy: cooldown active")
        return None

    usd_to_spend = usd_balance * cfg.recycler_sell_pct
    usd_to_spend = max(cfg.min_order_usd, min(usd_to_spend, cfg.max_order_usd))

    if usd_to_spend < cfg.min_order_usd:
        return None

    logger.info("USD recycler BUY: price %.1f%% below avg sell $%.2f → buying $%.2f of BTC",
                discount_pct * 100, avg_sell_basis, usd_to_spend)
    set_state("usd_recycler_last_buy_price", str(current_price))
    set_state("usd_recycler_waiting_resell", "true")
    return {"type": "buy", "usd_amount": usd_to_spend, "reason": "usd_recycler_buy",
            "discount_pct": discount_pct}


def usd_check_recycler_resell(cfg: Config, current_price: float,
                               btc_balance: float) -> dict | None:
    """
    USD mode: After a recycler buy, resell the BTC when price bounces.
    Mirror of btc_check_recycler_rebuy.
    """
    if get_state("usd_recycler_waiting_resell", "false") != "true":
        return None

    buy_price = float(get_state("usd_recycler_last_buy_price", "0") or 0)
    if buy_price <= 0:
        return None

    rise_from_buy = (current_price - buy_price) / buy_price
    if rise_from_buy < cfg.recycler_rebuy_drop_pct:
        logger.debug("USD recycler resell: waiting for %.1f%% rise from $%.2f (currently %.1f%%)",
                     cfg.recycler_rebuy_drop_pct * 100, buy_price, rise_from_buy * 100)
        return None

    # Sell the recycler BTC slice (same percentage we used to buy)
    btc_to_sell = btc_balance * cfg.recycler_sell_pct
    min_btc     = cfg.min_order_usd / current_price
    btc_to_sell = max(min_btc, btc_to_sell)

    logger.info("USD recycler RESELL: price rose %.1f%% from buy $%.2f → selling %.8f BTC",
                rise_from_buy * 100, buy_price, btc_to_sell)
    set_state("usd_recycler_waiting_resell", "false")
    return {"type": "sell", "btc_amount": btc_to_sell, "reason": "usd_recycler_resell",
            "rise_pct": rise_from_buy}


# ══════════════════════════════════════════════════════════════════════════════
#  SHARED UTILITY
# ══════════════════════════════════════════════════════════════════════════════

def get_usd_avg_sell_basis() -> float:
    """
    Calculate the average price at which the bot has sold BTC for USD
    (used by USD mode recycler logic).
    """
    from database import get_all_trades
    trades = get_all_trades()
    sell_trades = [t for t in trades
                   if t["side"] == "sell" and
                   t["reason"] in ("usd_dca_sell", "usd_spike_sell_tier1",
                                   "usd_spike_sell_tier2", "usd_spike_sell_tier3")]
    if not sell_trades:
        return 0.0
    total_btc = sum(t["btc_amount"] for t in sell_trades)
    total_usd = sum(t["usd_amount"] for t in sell_trades)
    return total_usd / total_btc if total_btc > 0 else 0.0
