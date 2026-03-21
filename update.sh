#!/bin/bash
# BotCoin update script — pulls latest code from GitHub and rebuilds containers
# Called by the botapi /api/update endpoint when user clicks "Update" in the dashboard
#
# IMPORTANT: This script must run on the HOST, not inside a container.
# It is triggered by botapi but executed by the host's init system via nohup
# so it survives when the botapi container shuts down during the rebuild.

REPO_DIR="/root/kraken-btc-bot"

echo "[update] Starting BotCoin update at $(date)" >> "$REPO_DIR/data/update.log"
cd "$REPO_DIR"

# Pull latest code (preserves .env and data/ — they are gitignored)
echo "[update] Pulling latest code from GitHub..." >> "$REPO_DIR/data/update.log"
git pull origin main >> "$REPO_DIR/data/update.log" 2>&1

echo "[update] Rebuilding containers..." >> "$REPO_DIR/data/update.log"
docker compose down >> "$REPO_DIR/data/update.log" 2>&1
docker compose up -d --build >> "$REPO_DIR/data/update.log" 2>&1

echo "[update] Update complete at $(date). New version: $(cat VERSION)" >> "$REPO_DIR/data/update.log"
