"""
price_history.py — Historical BTC/USD price lookups via CoinGecko.

Used to value historical deposits at the BTC price that prevailed on the day
each transfer hit Kraken. CoinGecko's free `/coins/bitcoin/history` endpoint
needs no API key but is rate limited (~30 calls/min on the free tier), so every
successful lookup is cached permanently in the `btc_price_history` table keyed
by calendar day. A given day's price never changes once the day is over, which
makes the cache safe to treat as authoritative after the first fetch.
"""

import logging
import time
import urllib.parse
import urllib.request
import json
from datetime import datetime, timezone

from database import get_connection

logger = logging.getLogger(__name__)

COINGECKO_HISTORY_URL = "https://api.coingecko.com/api/v3/coins/bitcoin/history"

# Free-tier courtesy delay between live CoinGecko calls. Cached days are free,
# so this only paces the initial backfill of never-before-seen deposit dates.
RATE_LIMIT_SLEEP_SECS = 2.0


def _to_date_key(timestamp_iso_or_date: str) -> str:
    """
    Normalise an ISO 8601 timestamp (or a bare YYYY-MM-DD date) to a UTC
    calendar-day key in YYYY-MM-DD form. This is the cache key and also the
    granularity CoinGecko's history endpoint supports.
    """
    raw = timestamp_iso_or_date.strip()
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        return raw
    # Tolerate a trailing 'Z' which datetime.fromisoformat() rejects pre-3.11.
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _cache_get(date_key: str):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT price_usd FROM btc_price_history WHERE date=?", (date_key,)
        ).fetchone()
    return row["price_usd"] if row else None


def _cache_put(date_key: str, price_usd: float):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO btc_price_history (date, price_usd)
            VALUES (?, ?)
            ON CONFLICT(date) DO UPDATE SET price_usd=excluded.price_usd
        """, (date_key, price_usd))


def _fetch_coingecko(date_key: str):
    """
    Fetch the BTC closing-ish USD price for a single UTC day from CoinGecko.
    Returns a float price or None on any failure (network, rate limit, missing
    data). CoinGecko expects the date as DD-MM-YYYY.
    """
    y, m, d = date_key.split("-")
    params = urllib.parse.urlencode({"date": f"{d}-{m}-{y}", "localization": "false"})
    req = urllib.request.Request(
        f"{COINGECKO_HISTORY_URL}?{params}",
        headers={"User-Agent": "BotCoin/1.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
        price = body.get("market_data", {}).get("current_price", {}).get("usd")
        return float(price) if price is not None else None
    except Exception as e:
        logger.warning("CoinGecko history lookup failed for %s: %s", date_key, e)
        return None


def get_btc_price_at(timestamp_iso_or_date: str):
    """
    Return the BTC/USD price on the UTC day of the given timestamp/date.

    Resolution order:
      1. Cache hit in `btc_price_history` → returned immediately (free).
      2. Cache miss → one CoinGecko call, result cached, then returned.

    Returns the price as a float, or None if CoinGecko has no data for that day
    (e.g. a date before BTC market data exists, or a transient API failure). The
    caller is responsible for the courtesy sleep between *uncached* lookups when
    backfilling many dates; a single call here does not sleep so cached reads
    stay instant.
    """
    date_key = _to_date_key(timestamp_iso_or_date)

    cached = _cache_get(date_key)
    if cached is not None:
        return cached

    price = _fetch_coingecko(date_key)
    if price is not None:
        _cache_put(date_key, price)
    return price
