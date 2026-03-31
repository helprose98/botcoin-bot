#!/usr/bin/env python3
"""
seed_price_history.py — Backfill daily_prices table with Kraken historical data.

Fetches up to 200 days of BTC/USD daily OHLC from Kraken's public API and
inserts them into the bot's database. Only inserts dates that don't already
exist — safe to run multiple times.

Usage:
    python3 /root/kraken-btc-bot/scripts/seed_price_history.py
"""

import sqlite3
import urllib.request
import json
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

DB_PATH = Path("/app/data/bot.db")

def fetch_kraken_daily_closes(days=200):
    """Fetch daily OHLC from Kraken public API. Returns list of (date_str, price)."""
    url = "https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1440"
    print(f"Fetching {days} days of BTC/USD daily prices from Kraken...")
    req = urllib.request.Request(url, headers={"User-Agent": "myBotCoin/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())

    if data.get("error"):
        raise RuntimeError(f"Kraken API error: {data['error']}")

    # Candle format: [time, open, high, low, close, vwap, volume, count]
    candles = (
        data["result"].get("XXBTZUSD") or
        data["result"].get("XBTUSD") or
        list(data["result"].values())[0]
    )

    # Take last `days` candles, extract date + close price
    results = []
    for candle in candles[-days:]:
        ts    = int(candle[0])
        close = float(candle[4])
        date  = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        results.append((date, close))

    print(f"  Got {len(results)} daily candles ({results[0][0]} → {results[-1][0]})")
    return results


def get_existing_dates(con):
    """Return set of dates already in daily_prices."""
    rows = con.execute("SELECT date FROM daily_prices").fetchall()
    return {r[0] for r in rows}


def seed(days=200):
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        print("Make sure the bot container is running and has created the DB.")
        return

    candles = fetch_kraken_daily_closes(days)

    with sqlite3.connect(DB_PATH) as con:
        existing = get_existing_dates(con)
        print(f"  Existing dates in DB: {len(existing)}")

        inserted = 0
        skipped  = 0
        for date, price in candles:
            if date in existing:
                skipped += 1
                continue
            con.execute(
                "INSERT INTO daily_prices (date, price_usd) VALUES (?, ?)",
                (date, price)
            )
            inserted += 1

        con.commit()

    print(f"\nDone.")
    print(f"  Inserted: {inserted} new days")
    print(f"  Skipped:  {skipped} (already existed)")
    print(f"  Total:    {inserted + skipped} days now in DB")
    print(f"\nThe bot will now have enough data to calculate the 200MA on its next cycle.")


if __name__ == "__main__":
    seed(200)
