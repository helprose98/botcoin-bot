"""
kraken_client.py — All communication with the Kraken REST API.
Handles authentication, balance queries, price fetching, and order placement.
"""

import hashlib
import hmac
import base64
import time
import urllib.parse
import logging
import requests
from decimal import Decimal, ROUND_DOWN

logger = logging.getLogger(__name__)

KRAKEN_API_URL = "https://api.kraken.com"
# Kraken uses "XBT" internally for Bitcoin, not "BTC"
PAIR_MAP = {
    "XBTUSD": "XXBTZUSD",
    "BTCUSD": "XXBTZUSD",
}

# ── Maker-only order parameters ────────────────────────────────────────────────
# Tick size for BTC/USD on Kraken (price precision = 1 decimal).
KRAKEN_BTCUSD_TICK = 0.1

# Hard sanity bound: a maker limit price may never drift more than this fraction
# from the last-trade price (protects against ticker corruption / a stale book).
MAKER_PRICE_MAX_DRIFT = 0.005  # 0.5%

# Maximum post-only submit attempts before giving up. Maker-only is final: there
# is no taker fallback. A missed fill is acceptable — the strategy re-evaluates
# on the next tick.
MAX_MAKER_RETRIES = 3

# Pause between a post-only rejection and the next reprice attempt, giving the
# book a moment to settle.
MAKER_POLL_INTERVAL_SECONDS = 2


def compute_maker_limit_price(side: str, best_bid: float, best_ask: float,
                              last_price: float, retry_count: int = 0) -> float:
    """
    Compute a post-only-safe limit price one or more ticks away from the near
    touch of the book.

    Buys rest BELOW best_bid (so they don't take the ask). Sells rest ABOVE
    best_ask (so they don't take the bid). Each retry steps one additional tick
    further from the book to handle book movement between fetch and submit.

    Raises ValueError if the resulting price would drift more than
    MAKER_PRICE_MAX_DRIFT from last_price (defensive guard against bad data).
    """
    offset_ticks = 1 + retry_count
    offset = KRAKEN_BTCUSD_TICK * offset_ticks
    if side == "buy":
        price = round(best_bid - offset, 1)
    elif side == "sell":
        price = round(best_ask + offset, 1)
    else:
        raise ValueError(f"Unknown side: {side}")

    drift = abs(price - last_price) / last_price
    if drift > MAKER_PRICE_MAX_DRIFT:
        raise ValueError(
            f"Maker price {price} drifts {drift:.2%} from last {last_price} "
            f"(max {MAKER_PRICE_MAX_DRIFT:.1%}); refusing to submit."
        )
    return price


def _is_post_only_rejection(err: "KrakenAPIError") -> bool:
    """Return True if a Kraken error indicates a post-only order would cross."""
    return "post only" in str(err).lower()


class KrakenClient:
    def __init__(self, api_key: str, api_secret: str, pair: str = "XBTUSD",
                 maker_fee: float = 0.0025, paper_trading: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.pair = PAIR_MAP.get(pair.upper(), pair.upper())
        self.maker_fee = maker_fee
        self.paper_trading = paper_trading
        if paper_trading:
            logger.warning("⚠️  PAPER TRADING MODE — no real orders will be placed")

    # ── Authentication ────────────────────────────────────────────────────────

    def _sign(self, urlpath: str, data: dict) -> str:
        """Generate Kraken API signature."""
        postdata = urllib.parse.urlencode(data)
        encoded = (str(data["nonce"]) + postdata).encode()
        message = urlpath.encode() + hashlib.sha256(encoded).digest()
        mac = hmac.new(base64.b64decode(self.api_secret), message, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    def _private(self, endpoint: str, params: dict = None) -> dict:
        """Make an authenticated private API call."""
        if params is None:
            params = {}
        urlpath = f"/0/private/{endpoint}"
        params["nonce"] = str(int(time.time() * 1000))
        headers = {
            "API-Key": self.api_key,
            "API-Sign": self._sign(urlpath, params),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            resp = requests.post(
                KRAKEN_API_URL + urlpath,
                data=params,
                headers=headers,
                timeout=30
            )
            resp.raise_for_status()
            result = resp.json()
            if result.get("error"):
                raise KrakenAPIError(result["error"])
            return result["result"]
        except requests.RequestException as e:
            logger.error("Kraken API request failed: %s", e)
            raise

    def _public(self, endpoint: str, params: dict = None) -> dict:
        """Make a public (unauthenticated) API call."""
        try:
            resp = requests.get(
                f"{KRAKEN_API_URL}/0/public/{endpoint}",
                params=params or {},
                timeout=30
            )
            resp.raise_for_status()
            result = resp.json()
            if result.get("error"):
                raise KrakenAPIError(result["error"])
            return result["result"]
        except requests.RequestException as e:
            logger.error("Kraken public API request failed: %s", e)
            raise

    # ── Market Data ───────────────────────────────────────────────────────────

    def get_ticker_price(self) -> float:
        """Return the current best ask price for the trading pair."""
        result = self._public("Ticker", {"pair": self.pair})
        # 'a' = ask [price, whole_lot_volume, lot_volume]
        # 'b' = bid
        # 'c' = last trade closed
        ticker = result[list(result.keys())[0]]
        price = float(ticker["c"][0])  # last trade price
        logger.debug("Current BTC price: $%.2f", price)
        return price

    def get_book_top(self) -> tuple[float, float, float]:
        """
        Return (best_bid, best_ask, last_price) from the Kraken Ticker.

        The same Ticker payload get_ticker_price() uses already carries the best
        bid ('b') and best ask ('a'); this returns all three so maker-only pricing
        can rest relative to the near touch of the book instead of the last trade.
        """
        result = self._public("Ticker", {"pair": self.pair})
        ticker = result[list(result.keys())[0]]
        best_bid  = float(ticker["b"][0])
        best_ask  = float(ticker["a"][0])
        last_price = float(ticker["c"][0])
        return best_bid, best_ask, last_price

    def get_ohlc(self, interval_minutes: int = 60, since: int = None) -> list:
        """Return OHLC candles. interval: 1,5,15,30,60,240,1440,10080,21600"""
        params = {"pair": self.pair, "interval": interval_minutes}
        if since:
            params["since"] = since
        result = self._public("OHLC", params)
        key = [k for k in result.keys() if k != "last"][0]
        return result[key]  # list of [time, open, high, low, close, vwap, volume, count]

    # ── Account ───────────────────────────────────────────────────────────────

    def get_balance(self) -> dict:
        """Return account balances as {asset: float}."""
        result = self._private("Balance")
        balances = {}
        for asset, amount in result.items():
            balances[asset] = float(amount)
        # Normalise XBT → BTC for ease of use
        balances["BTC"] = balances.get("XXBT", balances.get("XBT", 0.0))
        balances["USD"] = balances.get("ZUSD", 0.0)
        logger.debug("Balances — BTC: %.8f  USD: $%.2f",
                     balances["BTC"], balances["USD"])
        return balances

    def get_trade_history(self, start: int = None, end: int = None) -> list:
        """Fetch closed trade history to calculate existing cost basis."""
        params = {"trades": True}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        result = self._private("TradesHistory", params)
        trades = list(result.get("trades", {}).values())
        # Filter to BTC/USD pair only
        btc_trades = [t for t in trades if self.pair in (t.get("pair", ""))]
        logger.info("Fetched %d historical BTC trades from Kraken", len(btc_trades))
        return btc_trades

    def calculate_historical_cost_basis(self) -> tuple[float, float]:
        """
        Pull full trade history and calculate weighted average cost basis.
        Returns (avg_cost_basis_usd, total_btc_held_from_history).
        """
        trades = self.get_trade_history()
        total_btc = 0.0
        total_cost = 0.0
        for t in trades:
            vol   = float(t.get("vol", 0))
            price = float(t.get("price", 0))
            cost  = float(t.get("cost", 0))
            fee   = float(t.get("fee", 0))
            if t.get("type") == "buy":
                total_btc  += vol
                total_cost += cost + fee
            elif t.get("type") == "sell":
                total_btc  -= vol
                total_cost -= cost  # reduce basis proportionally (simplified)
        avg_basis = total_cost / total_btc if total_btc > 0 else 0.0
        logger.info("Historical cost basis: $%.2f avg | %.8f BTC total",
                    avg_basis, total_btc)
        return avg_basis, total_btc

    # ── Order Placement ───────────────────────────────────────────────────────

    def _truncate_btc(self, amount: float, decimals: int = 8) -> str:
        """Truncate BTC amount to allowed decimal places (Kraken: 8)."""
        d = Decimal(str(amount)).quantize(
            Decimal("0." + "0" * decimals), rounding=ROUND_DOWN
        )
        return str(d)

    def place_limit_buy(self, usd_amount: float, price: float,
                        reason: str = "buy") -> dict | bool:
        """
        Place a post-only (maker-only) limit buy order.

        usd_amount: how much USD to spend (fee accounted for in the BTC volume).
        price: the last-trade reference price from the caller. It is used as the
            paper-trading fill price; for LIVE orders the actual limit is derived
            from the live book so the order rests below best bid.

        All LIVE orders carry oflags=post and are retried up to MAX_MAKER_RETRIES
        times, stepping one tick further from the book each time Kraken rejects
        with "Post only order would have crossed". There is NO taker fallback:
        maker-only is intentional — we accept a missed fill in exchange for the
        maker fee, and the strategy re-evaluates next tick.

        Returns the standard order dict on success, or False on permanent failure
        (drift guard tripped, repeated post-only rejection, or API error).
        """
        if self.paper_trading:
            effective_usd = usd_amount / (1 + self.maker_fee)
            btc_str = self._truncate_btc(effective_usd / price)
            fee = usd_amount * self.maker_fee
            logger.info("[PAPER] Placing limit BUY: %.8f BTC @ $%.1f (USD: $%.2f)",
                        float(btc_str), price, usd_amount)
            return {
                "order_id":  f"PAPER-{int(time.time())}",
                "btc_amount": float(btc_str),
                "usd_amount": usd_amount,
                "price":      price,
                "fee_usd":    fee,
                "ordertype":  "limit-post",
                "paper":      True,
            }

        for attempt in range(MAX_MAKER_RETRIES):
            best_bid, best_ask, last_price = self.get_book_top()
            try:
                limit_price = compute_maker_limit_price(
                    "buy", best_bid, best_ask, last_price, retry_count=attempt
                )
            except ValueError as e:
                logger.warning("[maker] buy price drift guard tripped: %s", e)
                return False

            effective_usd = usd_amount / (1 + self.maker_fee)
            btc_str = self._truncate_btc(effective_usd / limit_price)
            try:
                result = self._private("AddOrder", {
                    "pair":      self.pair,
                    "type":      "buy",
                    "ordertype": "limit",
                    "price":     f"{limit_price:.1f}",
                    "volume":    btc_str,
                    "oflags":    "post",
                    "validate":  False,
                })
                order_id = result["txid"][0]
                fee = usd_amount * self.maker_fee
                logger.info("[maker] BUY attempt %d/%d accepted txid=%s @ $%.1f "
                            "(bid=%.1f ask=%.1f)", attempt + 1, MAX_MAKER_RETRIES,
                            order_id, limit_price, best_bid, best_ask)
                return {
                    "order_id":   order_id,
                    "btc_amount": float(btc_str),
                    "usd_amount": usd_amount,
                    "price":      limit_price,
                    "fee_usd":    fee,
                    "ordertype":  "limit-post",
                    "paper":      False,
                }
            except KrakenAPIError as e:
                if _is_post_only_rejection(e) and attempt < MAX_MAKER_RETRIES - 1:
                    logger.info("[maker] buy post-only rejected (attempt %d/%d): "
                                "%s — repricing", attempt + 1, MAX_MAKER_RETRIES, e)
                    time.sleep(MAKER_POLL_INTERVAL_SECONDS)
                    continue
                logger.error("[maker] buy AddOrder failed permanently: %s", e)
                return False
        return False

    def place_limit_sell(self, btc_amount: float, price: float,
                         reason: str = "sell") -> dict | bool:
        """
        Place a post-only (maker-only) limit sell order.

        btc_amount: how much BTC to sell.
        price: last-trade reference (paper fill price); LIVE limit is derived from
            the live book so the order rests above best ask.

        Identical maker-only semantics to place_limit_buy (oflags=post, up to
        MAX_MAKER_RETRIES reprices, no taker fallback). Returns the order dict on
        success or False on permanent failure.
        """
        btc_str = self._truncate_btc(btc_amount)

        if self.paper_trading:
            usd_gross = float(btc_str) * price
            fee = usd_gross * self.maker_fee
            logger.info("[PAPER] Placing limit SELL: %.8f BTC @ $%.1f (gross USD: $%.2f)",
                        float(btc_str), price, usd_gross)
            return {
                "order_id":  f"PAPER-{int(time.time())}",
                "btc_amount": float(btc_str),
                "usd_amount": usd_gross,
                "price":      price,
                "fee_usd":    fee,
                "ordertype":  "limit-post",
                "paper":      True,
            }

        for attempt in range(MAX_MAKER_RETRIES):
            best_bid, best_ask, last_price = self.get_book_top()
            try:
                limit_price = compute_maker_limit_price(
                    "sell", best_bid, best_ask, last_price, retry_count=attempt
                )
            except ValueError as e:
                logger.warning("[maker] sell price drift guard tripped: %s", e)
                return False

            usd_gross = float(btc_str) * limit_price
            fee = usd_gross * self.maker_fee
            try:
                result = self._private("AddOrder", {
                    "pair":      self.pair,
                    "type":      "sell",
                    "ordertype": "limit",
                    "price":     f"{limit_price:.1f}",
                    "volume":    btc_str,
                    "oflags":    "post",
                    "validate":  False,
                })
                order_id = result["txid"][0]
                logger.info("[maker] SELL attempt %d/%d accepted txid=%s @ $%.1f "
                            "(bid=%.1f ask=%.1f)", attempt + 1, MAX_MAKER_RETRIES,
                            order_id, limit_price, best_bid, best_ask)
                return {
                    "order_id":   order_id,
                    "btc_amount": float(btc_str),
                    "usd_amount": usd_gross,
                    "price":      limit_price,
                    "fee_usd":    fee,
                    "ordertype":  "limit-post",
                    "paper":      False,
                }
            except KrakenAPIError as e:
                if _is_post_only_rejection(e) and attempt < MAX_MAKER_RETRIES - 1:
                    logger.info("[maker] sell post-only rejected (attempt %d/%d): "
                                "%s — repricing", attempt + 1, MAX_MAKER_RETRIES, e)
                    time.sleep(MAKER_POLL_INTERVAL_SECONDS)
                    continue
                logger.error("[maker] sell AddOrder failed permanently: %s", e)
                return False
        return False

    def get_open_orders(self) -> dict:
        """Return all open orders."""
        return self._private("OpenOrders").get("open", {})

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True if successful."""
        try:
            self._private("CancelOrder", {"txid": order_id})
            logger.info("Cancelled order %s", order_id)
            return True
        except KrakenAPIError as e:
            logger.warning("Could not cancel order %s: %s", order_id, e)
            return False


class KrakenAPIError(Exception):
    """Raised when Kraken returns an error in the API response."""
    pass
