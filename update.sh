#!/bin/bash
# BotCoin host update script
# Runs on the HOST (not inside a container)
# Triggered by: touch /root/kraken-btc-bot/data/update.trigger
# Watched by: /etc/cron.d/botcoin-update (installed by setup)

REPO_DIR="/root/kraken-btc-bot"
LOG="$REPO_DIR/data/update.log"
TRIGGER="$REPO_DIR/data/update.trigger"

# Remove trigger file first so we don't loop
rm -f "$TRIGGER"

echo "[update] Starting BotCoin update at $(date)" > "$LOG"
cd "$REPO_DIR"

echo "[update] Pulling latest code from GitHub..." >> "$LOG"
git pull origin main >> "$LOG" 2>&1

echo "[update] Rebuilding containers..." >> "$LOG"
docker compose down >> "$LOG" 2>&1
docker compose up -d --build >> "$LOG" 2>&1

echo "[update] Complete at $(date). Version: $(cat VERSION)" >> "$LOG"
