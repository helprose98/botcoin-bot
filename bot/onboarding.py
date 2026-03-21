"""
onboarding.py — First-run setup.
Reads existing Kraken balance, pulls trade history, calculates cost basis,
and seeds the database so the bot starts with accurate state.
"""

import logging
from datetime import datetime, timezone

from database import (
    init_db, record_trade, save_portfolio_snapshot,
    get_state, set_state, get_latest_snapshot
)
from kraken_client import KrakenClient

logger = logging.getLogger(__name__)


def run_onboarding(client: KrakenClient) -> dict:
    """
    Execute first-run onboarding:
    1. Fetch current balances
    2. Pull historical trade history and compute cost basis
    3. Seed the database with an onboarding snapshot
    Returns summary dict.
    """
    already_done = get_state("onboarding_complete", "false")
    if already_done == "true":
        logger.info("Onboarding already complete, skipping")
        snapshot = get_latest_snapshot()
        return {
            "skipped": True,
            "btc_balance":    snapshot["btc_balance"] if snapshot else 0,
            "usd_balance":    snapshot["usd_balance"] if snapshot else 0,
            "avg_cost_basis": snapshot["avg_cost_basis"] if snapshot else 0,
        }

    logger.info("=" * 60)
    logger.info("FIRST RUN — Starting onboarding scan")
    logger.info("=" * 60)

    # Step 1: Current balances
    balances = client.get_balance()
    btc_balance = balances.get("BTC", 0.0)
    usd_balance = balances.get("USD", 0.0)

    logger.info("Current account balances:")
    logger.info("  BTC: %.8f", btc_balance)
    logger.info("  USD: $%.2f", usd_balance)

    # Step 2: Historical cost basis
    avg_cost_basis = 0.0
    historical_btc = 0.0
    total_fees_paid = 0.0

    if btc_balance > 0:
        logger.info("Fetching trade history to calculate cost basis...")
        try:
            avg_cost_basis, historical_btc = client.calculate_historical_cost_basis()
            logger.info("Calculated avg cost basis: $%.2f/BTC from history", avg_cost_basis)
        except Exception as e:
            logger.warning("Could not fetch full history: %s. Using $0 basis.", e)
            avg_cost_basis = 0.0

        # Record existing BTC as an onboarding pseudo-trade
        if btc_balance > 0 and avg_cost_basis > 0:
            usd_value = btc_balance * avg_cost_basis
            trade_id = record_trade(
                order_id   = f"ONBOARDING-{int(datetime.now(timezone.utc).timestamp())}",
                side       = "buy",
                reason     = "onboarding",
                btc_amount = btc_balance,
                usd_amount = usd_value,
                price_usd  = avg_cost_basis,
                fee_usd    = 0.0,  # fees already baked into historical cost basis
                paper_trade= False,
            )
            logger.info("Recorded existing %.8f BTC at $%.2f avg basis", btc_balance, avg_cost_basis)
        else:
            trade_id = None
    else:
        logger.info("No existing BTC found. Starting fresh with USD only.")
        trade_id = None

    # Step 3: Save initial snapshot
    save_portfolio_snapshot(
        trade_id       = trade_id,
        btc_balance    = btc_balance,
        usd_balance    = usd_balance,
        avg_cost_basis = avg_cost_basis,
        total_fees_paid= total_fees_paid,
    )

    set_state("onboarding_complete", "true")
    set_state("onboarding_btc_balance", str(btc_balance))
    set_state("onboarding_avg_cost_basis", str(avg_cost_basis))

    logger.info("=" * 60)
    logger.info("ONBOARDING COMPLETE")
    logger.info("  BTC balance:    %.8f BTC", btc_balance)
    logger.info("  USD balance:    $%.2f", usd_balance)
    logger.info("  Avg cost basis: $%.2f/BTC", avg_cost_basis)
    if avg_cost_basis > 0 and btc_balance > 0:
        current_value = btc_balance * avg_cost_basis  # placeholder, real price fetched in main
        logger.info("  Position value: ~$%.2f (at cost basis price)", current_value)
    logger.info("=" * 60)

    return {
        "skipped":        False,
        "btc_balance":    btc_balance,
        "usd_balance":    usd_balance,
        "avg_cost_basis": avg_cost_basis,
    }
