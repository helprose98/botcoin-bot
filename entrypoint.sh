#!/bin/bash
# entrypoint.sh — Bot container startup
# Runs one-time setup tasks before launching the bot

set -e

# Seed historical price data if needed (safe to run every time — skips existing dates)
echo "[startup] Seeding price history from Kraken..."
python3 /app/scripts/seed_price_history.py || echo "[startup] Price seed skipped or failed — continuing"

# Launch the bot
echo "[startup] Starting bot..."
exec python3 bot/main.py
