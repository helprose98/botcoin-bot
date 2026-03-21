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
                        reason: str = "buy") -> dict:
        """
        Place a limit buy order.
        usd_amount: how much USD to spend (fee will be deducted from this)
        price: limit price in USD
        Returns order details dict.
        """
        # Calculate BTC volume accounting for fee
        effective_usd = usd_amount / (1 + self.maker_fee)
        btc_volume = effective_usd / price
        btc_str = self._truncate_btc(btc_volume)
        price_str = f"{price:.1f}"

        logger.info("[%s] Placing limit BUY: %.8f BTC @ $%s (USD: $%.2f)",
                    "PAPER" if self.paper_trading else "LIVE",
                    float(btc_str), price_str, usd_amount)

        if self.paper_trading:
            fee = usd_amount * self.maker_fee
            return {
                "order_id": f"PAPER-{int(time.time())}",
                "btc_amount": float(btc_str),
                "usd_amount": usd_amount,
                "price": price,
                "fee_usd": fee,
                "paper": True,
            }

        result = self._private("AddOrder", {
            "pair":      self.pair,
            "type":      "buy",
            "ordertype": "limit",
            "price":     price_str,
            "volume":    btc_str,
            "validate":  False,
        })
        order_id = result["txid"][0]
        fee = usd_amount * self.maker_fee
        return {
            "order_id":   order_id,
            "btc_amount": float(btc_str),
            "usd_amount": usd_amount,
            "price":      price,
            "fee_usd":    fee,
            "paper":      False,
        }

    def place_limit_sell(self, btc_amount: float, price: float,
                         reason: str = "sell") -> dict:
        """
        Place a limit sell order.
        btc_amount: how much BTC to sell
        price: limit price in USD
        Returns order details dict.
        """
        btc_str   = self._truncate_btc(btc_amount)
        price_str = f"{price:.1f}"
        usd_gross = float(btc_str) * price
        fee       = usd_gross * self.maker_fee

        logger.info("[%s] Placing limit SELL: %.8f BTC @ $%s (gross USD: $%.2f)",
                    "PAPER" if self.paper_trading else "LIVE",
                    float(btc_str), price_str, usd_gross)

        if self.paper_trading:
            return {
                "order_id": f"PAPER-{int(time.time())}",
                "btc_amount": float(btc_str),
                "usd_amount": usd_gross,
                "price":      price,
                "fee_usd":    fee,
                "paper":      True,
            }

        result = self._private("AddOrder", {
            "pair":      self.pair,
            "type":      "sell",
            "ordertype": "limit",
            "price":     price_str,
            "volume":    btc_str,
            "validate":  False,
        })
        order_id = result["txid"][0]
        return {
            "order_id":   order_id,
            "btc_amount": float(btc_str),
            "usd_amount": usd_gross,
            "price":      price,
            "fee_usd":    fee,
            "paper":      False,
        }

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
