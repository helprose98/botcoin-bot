# BotCoin — Setup Guide

BotCoin is a Bitcoin DCA and accumulation bot that runs on your own server and connects to your Kraken account. It uses a two-server architecture: one server runs the trading bot, and a separate server runs the web dashboard.

---

## Architecture Overview

```
BotCoin-Bot (your Vultr server)        BotCoin-Dash (shared dashboard server)
┌─────────────────────────────┐        ┌──────────────────────────────┐
│  Setup wizard   (port 8081) │        │  Web dashboard  (port 8080)  │
│  Bot API        (port 8081) │◄───────┤  Connects to your bot IP     │
│  Trading bot                │        │  No keys stored here         │
│  Kraken API keys (private)  │        └──────────────────────────────┘
└─────────────────────────────┘
```

- **BotCoin-Bot** — your private trading server. Holds your Kraken API keys. Never shared.
- **BotCoin-Dash** — the web interface. Can be shared with friends. Has no access to keys.

---

## Option A — Deploy from Snapshot (Fastest)

If you have access to the BotCoin Vultr snapshot:

1. Log into Vultr → Deploy new server → Choose **Snapshot** → Select `BotCoin-v1.0-Clean`
2. Pick any plan ($6/month is fine), any region
3. Once deployed, browse to `http://YOUR_BOT_IP:8081`
4. Complete the 3-step setup wizard
5. Open BotCoin-Dash and connect using your bot IP and password

Done. Skip to **Step 3 — Connect the Dashboard** below.

---

## Option B — Fresh Install from GitHub

### Step 1 — Create the server
- Log into Vultr, deploy a new server
- OS: Ubuntu 24.04 LTS
- Plan: $6/month (1 vCPU, 1GB RAM) is sufficient
- Note the server's IP address

### Step 2 — Install and start the bot

SSH into the server and run these commands one at a time:

```bash
# Install Docker
curl -fsSL https://get.docker.com | sh

# Install git
apt-get install -y git

# Clone BotCoin
git clone https://github.com/helprose98/botcoin-bot.git /root/kraken-btc-bot

# Create required files
cd /root/kraken-btc-bot
touch .env
mkdir -p data logs

# Start the bot
docker compose up -d --build

# Install the one-click update watcher
bash /root/kraken-btc-bot/install-update-watcher.sh
```

### Step 3 — Complete the setup wizard

Browse to:
```
http://YOUR_BOT_IP:8081
```

The setup wizard walks you through:
1. Connecting your Kraken API keys
2. Setting your dashboard password

Default settings applied automatically:
- DCA: $50/week every Thursday at 9:00 AM EDT
- Mode: Auto (switches between BTC and USD accumulation based on market)

The bot trades real funds from the moment setup completes. All settings can be
changed anytime from the dashboard.

---

## Step 4 — Connect the Dashboard

Open **[https://dash.mybotcoin.io](https://dash.mybotcoin.io)** in your browser and enter:
- **Bot Server IP**: your bot server's IP address
- **Password**: the password you set in the wizard

The dashboard connects live to your bot and shows real-time data.

---

## Updating the Bot

### Normal path (dashboard)

When a new version is available, an orange **Update** badge appears in the dashboard header. Click it and confirm. This does not run anything inside the bot — it drops a marker file that a host-side watcher (a one-line cron job) picks up within ~60 seconds and runs `update.sh` on the host.

`update.sh` is safe by design:
- It **builds the new images first, while the old containers keep running**, then swaps. If the build fails, the old version stays up — no downtime.
- It **health-checks** `http://localhost:8081/api/health` for up to 60s after the swap. If the new version doesn't come up healthy, it **rolls back** to the previous version automatically.
- It is **idempotent**: re-clicking Update on an already-current bot is a no-op.

### Watching an update / reading the log

Every step is logged with timestamps. From SSH:

```bash
tail -f /root/kraken-btc-bot/logs/update.log
```

A machine-readable status the dashboard polls lives at `/root/kraken-btc-bot/data/update.status`.

### Manual update from SSH (if the dashboard is unreachable)

The dashboard button only writes the marker file. You can do the same thing by hand, or just run the update script directly:

```bash
cd /root/kraken-btc-bot
# Either trigger the watcher:
touch data/update.trigger
# ...or run the safe update script directly (recommended if you want to watch it):
bash update.sh
```

### If an update ever leaves the bot down

`update.sh` rolls back automatically, but if you ever see no containers (`docker ps` empty) the unconditional recovery is always:

```bash
cd /root/kraken-btc-bot && docker compose up -d --build
```

### First-time install of the update watcher

The watcher is installed automatically on a fresh install. To (re)install or verify it on an existing box — this is **idempotent**, safe to re-run:

```bash
bash /root/kraken-btc-bot/install-update-watcher.sh
# verify the cron drop-in exists:
cat /etc/cron.d/botcoin-update
```

---

## Default Settings Reference

| Setting | Default | Change in |
|---------|---------|-----------|
| DCA Amount | $50 | Dashboard → Settings |
| DCA Frequency | Weekly | Dashboard → Settings |
| DCA Day | Thursday | Dashboard → Settings |
| DCA Time | 9:00 AM EDT | Dashboard → Settings |
| Bot Mode | Auto | Dashboard → Settings |
| Aggression | Balanced | Dashboard → Settings |

---

## Troubleshooting

**Can't reach the setup wizard:**
```bash
docker ps  # confirm containers are running
```

**Bot not starting:**
```bash
docker logs kraken-btc-bot --tail 50
```

**Bot API not responding:**
```bash
docker logs kraken-btc-botapi --tail 50
```

**Force restart everything:**
```bash
cd /root/kraken-btc-bot && docker compose down && docker compose up -d --build
```

**Reset to factory (wipes all data and config):**
```bash
cd /root/kraken-btc-bot
docker compose down
rm -f .env
rm -rf data/ logs/
mkdir data logs
touch .env
docker compose up -d
```

---

## Kraken API Key Permissions Required

When creating API keys at Kraken, enable:
- ✅ Query Funds
- ✅ Query Open Orders & Trades
- ✅ Query Closed Orders & Trades
- ✅ Create & Modify Orders

Do NOT enable withdrawal permissions.
