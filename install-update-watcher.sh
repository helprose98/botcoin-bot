#!/bin/bash
# ── BotCoin update-watcher installer ──────────────────────────────────────────
# Run this ONCE on the host (as root) after the first deploy to enable one-click
# updates from the dashboard. It is fully idempotent — re-running it is safe and
# simply re-asserts the desired state.
#
#   Usage: bash /root/kraken-btc-bot/install-update-watcher.sh
#
# What it installs:
#   - Marks update.sh executable.
#   - Ensures the runtime data/ and logs/ directories exist.
#   - Installs a host cron entry (/etc/cron.d/botcoin-update) that, once per
#     minute, runs update.sh IF AND ONLY IF the dashboard has dropped a
#     data/update.trigger marker. The watcher runs on the HOST, outside the
#     containers, so it survives the container restart that an update performs.
#
# Why a host cron and not an in-container restart: the container running the API
# cannot reliably restart itself (it would kill the process mid-update). The host
# watcher is the supervising process that lives across the container swap.
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

REPO_DIR="/root/kraken-btc-bot"
CRON_FILE="/etc/cron.d/botcoin-update"

if [ ! -d "$REPO_DIR" ]; then
  echo "ERROR: $REPO_DIR does not exist. Clone the bot repo there first." >&2
  exit 1
fi

# Make the host update script executable (idempotent).
chmod +x "$REPO_DIR/update.sh"

# Ensure runtime dirs exist so the very first update has somewhere to log and
# to write its lock/trigger files (idempotent — mkdir -p never errors).
mkdir -p "$REPO_DIR/data" "$REPO_DIR/logs"

# Install / overwrite the host cron watcher. Writing the file unconditionally
# makes this idempotent: a re-run just rewrites identical content.
cat > "$CRON_FILE" << 'EOF'
# BotCoin update watcher — runs once per minute, but only does work when the
# dashboard has requested an update by creating the trigger file. update.sh is
# itself idempotent and single-flight (flock), so an overlapping minute is safe.
* * * * * root [ -f /root/kraken-btc-bot/data/update.trigger ] && /bin/bash /root/kraken-btc-bot/update.sh >> /root/kraken-btc-bot/logs/update.log 2>&1
EOF

chmod 644 "$CRON_FILE"

# Nudge cron to reload the drop-in (most distros pick /etc/cron.d up
# automatically, but a reload removes the up-to-a-minute lag on some systems).
# Best-effort: don't fail the install if the service name differs.
if command -v systemctl >/dev/null 2>&1; then
  systemctl reload cron 2>/dev/null || systemctl reload crond 2>/dev/null || true
fi

echo "Update watcher installed at $CRON_FILE."
echo "One-click updates from the dashboard are now active."
echo "Logs: tail -f $REPO_DIR/logs/update.log"
