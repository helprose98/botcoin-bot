#!/bin/bash
# Run this once on the host after first deploy to enable one-click updates from the dashboard
# Usage: bash install-update-watcher.sh

REPO_DIR="/root/kraken-btc-bot"

# Make update.sh executable
chmod +x "$REPO_DIR/update.sh"

# Install host cron that checks every minute for the trigger file
cat > /etc/cron.d/botcoin-update << 'EOF'
# BotCoin update watcher — checks every minute for dashboard-triggered updates
* * * * * root [ -f /root/kraken-btc-bot/data/update.trigger ] && /bin/bash /root/kraken-btc-bot/update.sh
EOF

chmod 644 /etc/cron.d/botcoin-update

echo "Update watcher installed. One-click updates from the dashboard are now active."
