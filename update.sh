#!/bin/bash
# BotCoin update script — pulls latest code from GitHub and rebuilds containers
# Called by the botapi /api/update endpoint when user clicks "Update" in the dashboard

set -e

REPO_DIR="/root/kraken-btc-bot"
GITHUB_REPO="https://github.com/helprose98/botcoin-bot.git"

echo "[update] Starting BotCoin update at $(date)"
cd "$REPO_DIR"

# Pull latest code (preserves .env and data/ — they are gitignored)
echo "[update] Pulling latest code from GitHub..."
git pull origin main

echo "[update] Rebuilding containers..."
docker compose down
docker compose up -d --build

echo "[update] Update complete at $(date)"
echo "[update] New version: $(cat VERSION)"
