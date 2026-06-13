"""
main.py — Bot entry point and main loop.

Startup sequence:
  1. Load config from .env
  2. Initialize database
  3. Run onboarding (first run only — reads Kraken balance + trade history)
  4. Enter main loop (every 5 minutes):
     a. Fetch current BTC price
     b. Sample price for history / 200MA
     c. Determine active mode (btc_accumulate / usd_accumulate / auto)
     d. Execute the appropriate strategy checks for that mode
     e. Sleep until next tick
"""

import logging
import time
import sys
import os
from datetime import datetime, timezone
from pathlib import Path

from config import load_config

ENV_PATH = Path("/app/.env")


def _reload_config_if_changed(current_cfg, last_mtime: float):
    """
    Check if .env has been modified since last load.
    If so, reload and return (new_cfg, new_mtime).
    Otherwise return (current_cfg, last_mtime) unchanged.
    """
    try:
        mtime = ENV_PATH.stat().st_mtime
        if mtime <= last_mtime:
            return current_cfg, last_mtime
        # Re-read .env into environment
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()
        new_cfg = load_config()
        import logging as _log
        _log.getLogger("main").info(
            "[config] .env changed — reloaded. DCA: $%.0f/%s @ %s UTC",
            new_cfg.dca_amount_usd, new_cfg.dca_frequency, new_cfg.dca_time_utc
        )
        return new_cfg, mtime
    except Exception as e:
        import logging as _log
        _log.getLogger("main").warning("[config] Reload failed: %s", e)
        return current_cfg, last_mtime


def _wait_for_config(retry_interval: int = 30) -> object:
    """
    Wait until a valid config can be loaded (i.e. .env has real API keys).
    Polls every retry_interval seconds instead of crashing on missing keys.
    This allows the setup wizard to write the .env after the container starts.
    """
    import time as _time
    # Basic stdout logging before the logging system is initialised
    _env_path = "/app/.env"
    while True:
        try:
            # Re-read the .env file into the environment on each attempt
            try:
                with open(_env_path) as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, _, v = line.partition("=")
                        os.environ.setdefault(k.strip(), v.strip())
            except FileNotFoundError:
                pass
            cfg = load_config()
            print("[startup] Config loaded successfully — starting bot.", flush=True)
            return cfg
        except (EnvironmentError, ValueError) as e:
            print(f"[startup] Waiting for configuration ({e}). "
                  f"Complete the setup wizard at port 8080. "
                  f"Retrying in {retry_interval}s...", flush=True)
            _time.sleep(retry_interval)


cfg = _wait_for_config()

# ── Logging ──────────────────────────────────────────────────────────────────
Path(cfg.log_file).parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, cfg.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S UTC",
    handlers=[
        logging.FileHandler(cfg.log_file),
        logging.StreamHandler(sys.stdout),
    ]
)
logging.Formatter.converter = time.gmtime  # UTC timestamps
logger = logging.getLogger("main")

# ── Application imports ───────────────────────────────────────────────────────
from database import (
    init_db, record_trade, save_portfolio_snapshot,
    get_latest_snapshot, record_price, record_daily_price, get_summary,
    record_mode_switch, get_last_trade_by_reason,
    add_range_position, close_range_position,
    get_unreconciled_trades, update_trade_fill, set_state,
)
from kraken_client import KrakenClient, KrakenAPIError
from onboarding import run_onboarding
from mode_manager import Mode, get_active_mode, get_mode_status
from strategies import (
    # BTC accumulate
    btc_check_dca, btc_check_dip_buy,
    btc_check_recycler_sell, btc_check_recycler_rebuy,
    # USD accumulate
    usd_check_dca, usd_check_spike_sell,
    usd_check_recycler_buy, usd_check_recycler_resell,
    get_usd_avg_sell_basis,
)
from sideways import check_sideways
from volatility import calculate_atr
import throttle

LOOP_INTERVAL_SECONDS  = 300   # Check every 5 minutes
PRICE_SAMPLE_INTERVAL  = 900   # Record intraday price every 15 min
DAILY_SAMPLE_HOUR_UTC  = 0     # Record daily price at midnight UTC (for 200MA)
STATUS_PRINT_INTERVAL  = 3600  # Print status summary every hour

# Maker-only orders rest on the book and may not fill in the same tick. A
# pending/open order older than this is treated as orphaned: it is canceled so
# stale resting orders don't tie up capital indefinitely.
PENDING_ORDER_ORPHAN_SECONDS = 24 * 3600

# Taker fee baseline used only for reconciler logging context; the real fee is
# read back from Kraken. Kept here as a named constant (no magic numbers).
TAKER_FEE_BASELINE = 0.0026

_last_intraday_sample = 0.0
_last_daily_sample_date = ""
_last_status_print = 0.0
_last_active_mode: Mode | None = None


# ── Order execution helpers ───────────────────────────────────────────────────

def execute_buy(client: KrakenClient, current_price: float, usd_amount: float,
                reason: str, active_mode: Mode, snapshot) -> bool:
    """Place a limit buy order, record it, update the portfolio snapshot."""
    try:
        limit_price = round(current_price * 0.9995, 1)
        order = client.place_limit_buy(usd_amount, limit_price, reason)
        if not order:
            return False

        prev_cost = snapshot["avg_cost_basis"] if snapshot else 0.0
        prev_fees = snapshot["total_fees_paid"] if snapshot else 0.0
        order_cost = order["usd_amount"] + order["fee_usd"]

        # Fetch live balances to update snapshot accurately
        try:
            balances = client.get_balance()
            live_btc = balances["BTC"]
            live_usd = balances["USD"]
        except Exception:
            # Fall back: estimate from snapshot
            live_btc = (snapshot["btc_balance"] if snapshot else 0.0) + order["btc_amount"]
            live_usd = max(0.0, (snapshot["usd_balance"] if snapshot else 0) - usd_amount)

        # Cost basis uses live BTC balance (most accurate denominator)
        prev_btc  = max(0.0, live_btc - order["btc_amount"])  # what we had before this buy
        new_btc   = live_btc
        new_basis = ((prev_btc * prev_cost) + order_cost) / new_btc if new_btc > 0 else order["price"]

        is_paper = order.get("paper", cfg.paper_trading)
        trade_id = record_trade(
            order_id    = order["order_id"],
            side        = "buy",
            reason      = reason,
            btc_amount  = order["btc_amount"],
            usd_amount  = order["usd_amount"],
            price_usd   = order["price"],
            fee_usd     = order["fee_usd"],
            active_mode = active_mode.value,
            paper_trade = is_paper,
            ordertype   = order.get("ordertype", "limit-post"),
            # Paper orders fill synthetically; live maker orders rest until the
            # reconciler confirms them via QueryOrders.
            fill_status = "closed" if is_paper else "pending",
        )
        save_portfolio_snapshot(
            trade_id        = trade_id,
            btc_balance     = live_btc,
            usd_balance     = live_usd,
            avg_cost_basis  = new_basis,
            total_fees_paid = prev_fees + order["fee_usd"],
        )
        throttle.record_trade_for_throttle()
        logger.info("BUY ✓ | %.8f BTC @ $%.2f | mode=%s | reason=%s | new basis=$%.2f",
                    order["btc_amount"], order["price"],
                    active_mode.value, reason, new_basis)
        return True

    except KrakenAPIError as e:
        logger.error("Kraken API error on buy (%s): %s", reason, e)
        return False
    except Exception as e:
        logger.exception("Unexpected error on buy (%s): %s", reason, e)
        return False


def execute_sell(client: KrakenClient, current_price: float, btc_amount: float,
                 reason: str, active_mode: Mode, snapshot) -> bool:
    """Place a limit sell order, record it, update the portfolio snapshot."""
    try:
        limit_price = round(current_price * 1.0005, 1)
        order = client.place_limit_sell(btc_amount, limit_price, reason)
        if not order:
            return False

        prev_fees = snapshot["total_fees_paid"] if snapshot else 0.0

        try:
            balances = client.get_balance()
            live_btc = balances["BTC"]
            live_usd = balances["USD"]
        except Exception:
            live_btc = max(0.0, (snapshot["btc_balance"] if snapshot else 0) - btc_amount)
            live_usd = (snapshot["usd_balance"] if snapshot else 0) + order["usd_amount"]

        is_paper = order.get("paper", cfg.paper_trading)
        trade_id = record_trade(
            order_id    = order["order_id"],
            side        = "sell",
            reason      = reason,
            btc_amount  = order["btc_amount"],
            usd_amount  = order["usd_amount"],
            price_usd   = order["price"],
            fee_usd     = order["fee_usd"],
            active_mode = active_mode.value,
            paper_trade = is_paper,
            ordertype   = order.get("ordertype", "limit-post"),
            fill_status = "closed" if is_paper else "pending",
        )
        save_portfolio_snapshot(
            trade_id        = trade_id,
            btc_balance     = live_btc,
            usd_balance     = live_usd,
            avg_cost_basis  = snapshot["avg_cost_basis"] if snapshot else 0,
            total_fees_paid = prev_fees + order["fee_usd"],
        )
        throttle.record_trade_for_throttle()
        logger.info("SELL ✓ | %.8f BTC @ $%.2f | mode=%s | reason=%s",
                    order["btc_amount"], order["price"], active_mode.value, reason)
        return True

    except KrakenAPIError as e:
        logger.error("Kraken API error on sell (%s): %s", reason, e)
        return False
    except Exception as e:
        logger.exception("Unexpected error on sell (%s): %s", reason, e)
        return False


# ── Range Recycler execution ──────────────────────────────────────────────────

def run_range_recycler(client, current_price, snapshot, active_mode):
    """Execute Range Recycler actions from the sideways overlay.

    Sideways trades are subject to the global throttle: they can otherwise stack
    on top of a BTC/USD strategy trade in the same tick, which is exactly the
    cross-strategy thrash the dampener exists to prevent.
    """
    actions = check_sideways(cfg, current_price, active_mode)

    for action in actions:
        allowed, msg = throttle.check_throttle(action["reason"], cfg)
        if not allowed:
            logger.info("[throttle] skipping %s: %s", action["reason"], msg)
            continue
        if action["type"] == "buy":
            ok = execute_buy(client, current_price, action["usd_amount"],
                             action["reason"], active_mode, snapshot)
            if ok and "position_id" in action:
                # Closing a USD-mode position (bought back cheaper)
                close_range_position(action["position_id"])
            elif ok:
                # Opening a new BTC-mode position
                btc_bought = action["usd_amount"] / current_price
                trade_row = get_last_trade_by_reason("range_recycler_buy")
                trade_id = trade_row["id"] if trade_row else None
                add_range_position(
                    trade_id=trade_id,
                    buy_price=current_price,
                    btc_amount=btc_bought,
                    usd_amount=action["usd_amount"],
                )

        elif action["type"] == "sell":
            ok = execute_sell(client, current_price, action["btc_amount"],
                              action["reason"], active_mode, snapshot)
            if ok and "position_id" in action:
                # Closing a BTC-mode position (sold at profit)
                close_range_position(action["position_id"])
            elif ok:
                # Opening a new USD-mode position (sold, will buy back cheaper)
                usd_amount = action["btc_amount"] * current_price
                trade_row = get_last_trade_by_reason("range_recycler_sell")
                trade_id = trade_row["id"] if trade_row else None
                add_range_position(
                    trade_id=trade_id,
                    buy_price=current_price,
                    btc_amount=action["btc_amount"],
                    usd_amount=usd_amount,
                )


# ── Strategy dispatch ─────────────────────────────────────────────────────────

def _dispatch_buy(client, current_price, action, active_mode, snapshot):
    """Apply the global throttle, then place a buy. Returns True if a trade fired."""
    allowed, msg = throttle.check_throttle(action["reason"], cfg)
    if not allowed:
        logger.info("[throttle] skipping %s: %s", action["reason"], msg)
        return False
    return execute_buy(client, current_price, action["usd_amount"],
                       action["reason"], active_mode, snapshot)


def _dispatch_sell(client, current_price, action, active_mode, snapshot):
    """Apply the global throttle, then place a sell. Returns True if a trade fired."""
    allowed, msg = throttle.check_throttle(action["reason"], cfg)
    if not allowed:
        logger.info("[throttle] skipping %s: %s", action["reason"], msg)
        return False
    return execute_sell(client, current_price, action["btc_amount"],
                        action["reason"], active_mode, snapshot)


def run_btc_accumulate_strategies(client, current_price, snapshot, active_mode,
                                  vol_multiplier=1.0):
    """Run all BTC accumulation strategy checks in priority order."""
    # Always fetch live balances from Kraken — never trust stale snapshot values.
    # The snapshot can lag after failed orders (e.g. EOrder:Insufficient funds)
    # causing the bot to repeatedly attempt orders it can't afford.
    try:
        live_balances = client.get_balance()
        btc_balance = live_balances["BTC"]
        usd_balance = live_balances["USD"]
        logger.debug("Live balances: BTC=%.8f USD=$%.2f", btc_balance, usd_balance)
    except Exception as e:
        logger.warning("Could not fetch live balance, falling back to snapshot: %s", e)
        btc_balance = snapshot["btc_balance"]    if snapshot else 0.0
        usd_balance = snapshot["usd_balance"]    if snapshot else 0.0
    avg_cost    = snapshot["avg_cost_basis"] if snapshot else 0.0

    # Priority 1: Recycler rebuy (cash is sitting waiting, deploy it)
    action = btc_check_recycler_rebuy(cfg, current_price, usd_balance)
    if action:
        return _dispatch_buy(client, current_price, action, active_mode, snapshot)

    # Priority 2: Recycler sell (take partial profit, reload reserve)
    action = btc_check_recycler_sell(cfg, current_price, btc_balance, avg_cost)
    if action:
        return _dispatch_sell(client, current_price, action, active_mode, snapshot)

    # Priority 3: Dip buy (deploy reserve on significant drop)
    action = btc_check_dip_buy(cfg, current_price, usd_balance, vol_multiplier)
    if action:
        return _dispatch_buy(client, current_price, action, active_mode, snapshot)

    # Priority 4: Scheduled DCA
    action = btc_check_dca(cfg, usd_balance)
    if action:
        return _dispatch_buy(client, current_price, action, active_mode, snapshot)

    return False


def run_usd_accumulate_strategies(client, current_price, snapshot, active_mode,
                                  vol_multiplier=1.0):
    """Run all USD accumulation strategy checks in priority order (inverted)."""
    # Same as BTC mode — always use live Kraken balances, not stale snapshot
    try:
        live_balances = client.get_balance()
        btc_balance = live_balances["BTC"]
        usd_balance = live_balances["USD"]
        logger.debug("Live balances: BTC=%.8f USD=$%.2f", btc_balance, usd_balance)
    except Exception as e:
        logger.warning("Could not fetch live balance, falling back to snapshot: %s", e)
        btc_balance = snapshot["btc_balance"] if snapshot else 0.0
        usd_balance = snapshot["usd_balance"] if snapshot else 0.0
    avg_sell_basis = get_usd_avg_sell_basis()

    # Priority 1: Recycler resell (BTC is waiting to be resold at bounce)
    action = usd_check_recycler_resell(cfg, current_price, btc_balance)
    if action:
        return _dispatch_sell(client, current_price, action, active_mode, snapshot)

    # Priority 2: Recycler buy (buy cheap BTC to resell higher for more USD)
    action = usd_check_recycler_buy(cfg, current_price, usd_balance, avg_sell_basis)
    if action:
        return _dispatch_buy(client, current_price, action, active_mode, snapshot)

    # Priority 3: Spike sell (sell BTC reserve on significant price pump)
    action = usd_check_spike_sell(cfg, current_price, btc_balance, vol_multiplier)
    if action:
        return _dispatch_sell(client, current_price, action, active_mode, snapshot)

    # DCA is intentionally suspended during USD accumulation mode.
    # DCA means "buy BTC on a schedule" — it doesn't invert.
    # The recycler and spike-sell system handles USD accumulation trading.
    logger.debug("USD mode: DCA halted — resumes when bot switches to BTC accumulation")

    return False


# ── Pending-order reconciliation ───────────────────────────────────────────────

def reconcile_pending_trades(client: KrakenClient):
    """
    Read back fills for resting maker orders and settle their trade rows.

    Post-only orders rest on the book and may not fill on the tick they were
    placed. Each unreconciled (pending/open, non-paper) trade row is looked up via
    QueryOrders and its fill_status/fee_actual/price_actual/was_maker columns are
    updated once Kraken reports a terminal state:
      - closed              → record actual fee/price; was_maker from oflags.
      - open                → still resting; leave for a later tick (orphan-cancel
                              if it has been waiting longer than the orphan window).
      - canceled / expired  → settle as such; not counted as a real fill.

    Runs at the top of every loop tick. Any per-order error is logged and skipped
    so one bad row never blocks the rest of the loop.
    """
    pending = get_unreconciled_trades()
    if not pending:
        return

    now = time.time()
    for row in pending:
        order_id = row["order_id"]
        try:
            resp = client._private("QueryOrders", {"txid": order_id})
        except KrakenAPIError as e:
            logger.warning("[reconcile] QueryOrders failed for %s: %s", order_id, e)
            continue
        except Exception as e:
            logger.warning("[reconcile] unexpected error for %s: %s", order_id, e)
            continue

        info = resp.get(order_id)
        if not info:
            logger.debug("[reconcile] no order info for %s yet", order_id)
            continue

        status = info.get("status", "")
        if status == "closed":
            oflags = info.get("oflags", "") or ""
            was_maker = 1 if "post" in oflags else 0
            fee_actual = float(info.get("fee", 0) or 0)
            price_actual = float(info.get("price", 0) or 0) or None
            update_trade_fill(row["id"], "closed", was_maker=was_maker,
                              fee_actual=fee_actual, price_actual=price_actual)
            logger.info("[reconcile] %s closed | maker=%d fee=$%.4f price=%s",
                        order_id, was_maker, fee_actual,
                        f"${price_actual:.2f}" if price_actual else "n/a")

        elif status == "open":
            update_trade_fill(row["id"], "open")
            # Orphan-cancel: clean up orders resting longer than the window.
            try:
                opened = float(info.get("opentm", 0) or 0)
            except (TypeError, ValueError):
                opened = 0.0
            if opened and (now - opened) > PENDING_ORDER_ORPHAN_SECONDS:
                logger.warning("[reconcile] %s open > %dh — canceling orphan",
                               order_id, PENDING_ORDER_ORPHAN_SECONDS // 3600)
                client.cancel_order(order_id)
                update_trade_fill(row["id"], "canceled")

        elif status in ("canceled", "expired"):
            update_trade_fill(row["id"], status)
            logger.info("[reconcile] %s %s — not counted as a fill", order_id, status)

        else:
            logger.debug("[reconcile] %s status=%s (no action)", order_id, status)


# ── Status reporting ──────────────────────────────────────────────────────────

def print_status(current_price: float, active_mode: Mode):
    summary  = get_summary()
    snapshot = get_latest_snapshot()
    mode_info = get_mode_status(current_price)
    if not snapshot:
        return

    btc_val  = snapshot["btc_balance"] * current_price
    basis    = snapshot["avg_cost_basis"]
    pnl_pct  = ((current_price - basis) / basis * 100) if basis > 0 else 0
    ma_str   = f"${mode_info['price_200ma']:,.0f}" if mode_info["price_200ma"] else f"building ({mode_info['days_of_history']}d/200d)"

    logger.info("━" * 65)
    logger.info("MODE    : %s | configured=%s",
                active_mode.value.upper(), cfg.mode.upper())
    logger.info("PRICE   : $%.2f  |  200MA: %s  |  deviation: %s%%",
                current_price, ma_str,
                f"{mode_info['ma_deviation_pct']:+.1f}" if mode_info["ma_deviation_pct"] is not None else "n/a")
    logger.info("PORTFOLIO: BTC=%.8f ($%.2f)  USD=$%.2f  basis=$%.2f  P&L=%+.1f%%",
                snapshot["btc_balance"], btc_val,
                snapshot["usd_balance"], basis, pnl_pct)
    logger.info("TOTALS  : trades=%d  fees=$%.4f  net_btc=%.8f  net_usd=$%.2f",
                summary["trade_count"], summary["total_fees_paid"],
                summary["net_btc_from_bot"], summary["net_usd_from_bot"])
    logger.info("SWITCHES: %d mode switches since start", summary["mode_switches"])
    logger.info("━" * 65)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    global cfg, _last_intraday_sample, _last_daily_sample_date
    global _last_status_print, _last_active_mode

    logger.info("=" * 65)
    logger.info("Kraken BTC Accumulation Bot — Starting")
    logger.info("Configured mode : %s", cfg.mode.upper())
    logger.info("Trading mode    : %s", "PAPER TRADING" if cfg.paper_trading else "LIVE TRADING")
    logger.info("=" * 65)

    init_db()

    client = KrakenClient(
        api_key       = cfg.api_key,
        api_secret    = cfg.api_secret,
        pair          = cfg.trading_pair,
        maker_fee     = cfg.maker_fee,
        paper_trading = cfg.paper_trading,
    )

    run_onboarding(client)

    logger.info("Entering main loop (interval: %ds)", LOOP_INTERVAL_SECONDS)
    consecutive_errors = 0
    MAX_ERRORS = 10
    _env_mtime = ENV_PATH.stat().st_mtime if ENV_PATH.exists() else 0.0

    while True:
        try:
            now = datetime.now(timezone.utc)

            # ── Hot-reload config if .env changed ────────────────────────────
            cfg, _env_mtime = _reload_config_if_changed(cfg, _env_mtime)

            # ── Fetch price ──────────────────────────────────────────────────
            current_price = client.get_ticker_price()

            # ── Price sampling ────────────────────────────────────────────────
            # Intraday sample (every 15 min — used for dip/spike detection)
            if time.time() - _last_intraday_sample >= PRICE_SAMPLE_INTERVAL:
                record_price(current_price)
                _last_intraday_sample = time.time()

            # Daily sample (once per day at midnight UTC — used for 200MA)
            today_str = now.strftime("%Y-%m-%d")
            if today_str != _last_daily_sample_date:
                record_daily_price(current_price)
                _last_daily_sample_date = today_str
                logger.debug("Daily price recorded: $%.2f (%s)", current_price, today_str)

            # ── Reconcile resting maker orders from prior ticks ───────────────
            reconcile_pending_trades(client)

            # ── Volatility-adaptive threshold multiplier ──────────────────────
            # Computed once per tick and propagated into dip/spike strategies.
            # Degrades to 1.0 (flat thresholds) on any failure or when disabled.
            if cfg.volatility_adaptive_enabled:
                atr_pct, atr_baseline_pct, vol_multiplier = calculate_atr(
                    client, cfg.vol_multiplier_min, cfg.vol_multiplier_max
                )
            else:
                atr_pct, atr_baseline_pct, vol_multiplier = None, None, 1.0
            set_state("vol_multiplier", vol_multiplier)
            set_state("atr_pct", atr_pct if atr_pct is not None else "")
            set_state("atr_baseline_pct",
                      atr_baseline_pct if atr_baseline_pct is not None else "")

            # ── Determine active mode ─────────────────────────────────────────
            active_mode = get_active_mode(cfg, current_price)

            # Log mode switches
            if _last_active_mode and active_mode != _last_active_mode:
                record_mode_switch(
                    from_mode  = _last_active_mode.value,
                    to_mode    = active_mode.value,
                    reason     = "auto_trend" if cfg.mode == "auto" else "config_change",
                    price_usd  = current_price,
                    ma200      = get_mode_status(current_price).get("price_200ma"),
                )
            _last_active_mode = active_mode

            # ── Status print (hourly) ─────────────────────────────────────────
            if time.time() - _last_status_print >= STATUS_PRINT_INTERVAL:
                print_status(current_price, active_mode)
                _last_status_print = time.time()

            # ── Get current snapshot ──────────────────────────────────────────
            snapshot = get_latest_snapshot()

            # ── Dispatch to correct strategy set ─────────────────────────────
            if active_mode == Mode.BTC_ACCUMULATE:
                run_btc_accumulate_strategies(client, current_price, snapshot,
                                              active_mode, vol_multiplier)
            elif active_mode == Mode.USD_ACCUMULATE:
                run_usd_accumulate_strategies(client, current_price, snapshot,
                                              active_mode, vol_multiplier)
            # AUTO mode is resolved to BTC or USD above — never reaches here as AUTO

            # ── Sideways Market overlay (Range Recycler) ─────────────────────
            run_range_recycler(client, current_price, snapshot, active_mode)

            consecutive_errors = 0

        except KrakenAPIError as e:
            consecutive_errors += 1
            logger.error("Kraken API error (%d/%d): %s", consecutive_errors, MAX_ERRORS, e)
            if consecutive_errors >= MAX_ERRORS:
                logger.critical("Too many API errors. Pausing 1 hour.")
                time.sleep(3600)
                consecutive_errors = 0

        except Exception as e:
            consecutive_errors += 1
            logger.exception("Unexpected error (%d/%d): %s", consecutive_errors, MAX_ERRORS, e)
            if consecutive_errors >= MAX_ERRORS:
                logger.critical("Too many errors. Pausing 1 hour.")
                time.sleep(3600)
                consecutive_errors = 0

        time.sleep(LOOP_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
