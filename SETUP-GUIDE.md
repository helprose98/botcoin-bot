# BotCoin — Setup Guide

BotCoin is a Bitcoin DCA and accumulation bot that runs on your own server and connects to your Kraken account. It uses a two-server architecture: one server runs the trading bot, and a separate server runs the web dashboard.

---

## Architecture Overview

```
BotCoin-Bot (your Vultr server)        BotCoin-Dash (shared dashboard server)
┌─────────────────────────────┐        ┌──────────────────────────────┐
│  Trading bot (port 8080)    │        │  Web dashboard (port 8080)   │
│  Bot API     (port 8081) ◄──┼────────┼── Connects to your bot IP    │
│  Kraken API keys (private)  │        │  No keys stored here         │
└─────────────────────────────┘        └──────────────────────────────┘
```

- **BotCoin-Bot** — your private trading server. Holds your Kraken API keys. Never shared.
- **BotCoin-Dash** — the web interface. Can be shared with friends. Has no access to keys.

---

## Requirements

- Two [Vultr](https://vultr.com) Ubuntu 24.04 servers (the $6/month plan works fine)
- A [Kraken](https://kraken.com) account with some USD balance
- Kraken API keys with **Query Funds**, **Query Orders**, and **Create & Modify Orders** permissions

---

## Step 1 — Set Up BotCoin-Bot

### 1.1 Create the server
- Log in to Vultr, deploy a new server
- OS: Ubuntu 24.04 LTS
- Plan: $6/month (1 vCPU, 1GB RAM) is sufficient
- Note the server's IP address

### 1.2 Install Docker
SSH into the server and run:
```bash
curl -fsSL https://get.docker.com | sh
```

### 1.3 Upload the bot files
From your Windows PC (PowerShell):
```powershell
scp C:\temp\kraken-btc-bot.zip root@YOUR_BOT_IP:/root/kraken-btc-bot.zip
```

### 1.4 Extract and start
```bash
cd /root && unzip kraken-btc-bot.zip
cd /root/kraken-btc-bot
docker compose up -d --build
```

### 1.5 Complete setup wizard
Open your browser and go to:
```
http://YOUR_BOT_IP:8080
```
The setup wizard will walk you through entering your Kraken API keys, DCA settings, and dashboard password. This writes your `.env` file and starts the bot automatically.

---

## Step 2 — Set Up BotCoin-Dash

### 2.1 Create a second server
- Same specs as above
- Note this server's IP address separately

### 2.2 Install Docker
```bash
curl -fsSL https://get.docker.com | sh
```

### 2.3 Upload dashboard files
```powershell
scp C:\temp\botcoin-dashboard.zip root@YOUR_DASH_IP:/root/botcoin-dashboard.zip
```

### 2.4 Extract and start
```bash
cd /root && unzip botcoin-dashboard.zip
cd /root/botcoin-dashboard
docker compose up -d --build
```

### 2.5 Connect to your bot
Open your browser and go to:
```
http://YOUR_DASH_IP:8080
```
Enter your **Bot Server IP** and the **dashboard password** you set in the wizard.

---

## Step 3 — Set Up Firewall (Vultr)

In Vultr, create a firewall group and attach both servers:

| Rule | Protocol | Port | Source |
|------|----------|------|--------|
| SSH | TCP | 22 | Your IP only |
| Dashboard | TCP | 8080 | Anywhere |
| Bot API | TCP | 8081 | Your Dash server IP only |

This ensures the bot API is only accessible from your dashboard server, not the public internet.

---

## DCA Settings

| Setting | Description | Default |
|---------|-------------|---------|
| `DCA_AMOUNT_USD` | How much USD to buy per DCA | $100 |
| `DCA_FREQUENCY` | How often: `daily`, `weekly`, `monthly` | `weekly` |
| `DCA_DAY` | Day of week (weekly only) | `monday` |
| `DCA_DAY_OF_MONTH` | Day of month (monthly only, 1-28) | `1` |
| `DCA_TIME_UTC` | Time of day in UTC (e.g. `13:00`) | `13:00` |

All settings can be changed from the dashboard Settings tab without restarting the bot.

---

## Updating the Bot

When a new version is available:
1. Download the new `kraken-btc-bot.zip`
2. Upload to your server: `scp C:\temp\kraken-btc-bot.zip root@YOUR_BOT_IP:/root/`
3. Run: `cd /root && unzip -o kraken-btc-bot.zip && cd /root/kraken-btc-bot && docker compose down && docker compose up -d --build`

Your `.env` file and database are preserved automatically — no settings are lost on update.

---

## Troubleshooting

**Bot not starting:**
```bash
docker logs kraken-btc-bot --tail 50
```

**Bot API not responding:**
```bash
docker logs kraken-btc-botapi --tail 50
```

**Check all containers are running:**
```bash
docker ps
```

**Force restart everything:**
```bash
cd /root/kraken-btc-bot && docker compose down && docker compose up -d --build
```
