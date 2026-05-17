# Colossus Trading Alert Bot

Monitors two Polymarket wallets for new trades and sends Telegram alerts with a copy-trade recommendation score.

## Watched Wallets (pre-configured)

| Label | Address |
|-------|---------|
| ColossusRN | `0x2005d16a84ceefa912d4e380cd32e7ff827875ea` |
| ColossusShark | `0x751a2b86cab503496efd325c8344e10159349ea1` |

---

## Quick Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Open `.env` and fill in two values:

```
TELEGRAM_BOT_TOKEN=   ← from @BotFather on Telegram
TELEGRAM_CHAT_ID=     ← from @userinfobot on Telegram
```

The wallets are already set. Everything else has sensible defaults.

### 3. Run the bot

```bash
python main.py
```

Leave the terminal open. The bot will:
- Send a startup message to your Telegram
- Poll every 30 seconds for new trades
- Alert you immediately when either wallet trades

---

## Alert Format

Each alert contains:

```
🔔 New Trade Detected

👤 Wallet: Trader_A (0x200...)
📊 Market: Will Trump win in 2024?
🎯 Outcome: YES

📈 BUY  |  Price: 62.0¢  |  Size: 500 shares
💵 Value: $310.00

📋 Trader Stats
  Win Rate: 68.0%  |  Vol: $45,230  |  Trades: 82

⚡ Score: 80/100
🟢 STRONG BUY — copy this trade
```

### Recommendation key

| Label | Meaning |
|-------|---------|
| 🟢 STRONG BUY | Score ≥ 75, solid win rate & volume — consider copying |
| 🟡 CONSIDER | Score 55–74 — trader stats are decent |
| 🔴 SKIP | Score < 55 — not enough track record to follow |

---

## Other Commands

**Check open positions** (prints and exits):
```bash
python main.py --positions
```

**Resolve a Polymarket username to wallet address:**
```bash
python main.py --lookup some_username
```

---

## Configuration Reference (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | *required* | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | *required* | Your Telegram user/chat ID |
| `WATCHED_WALLETS` | pre-set | Comma-separated wallet addresses |
| `WALLET_LABELS` | `ColossusRN,ColossusShark` | Friendly names shown in alerts |
| `POLL_INTERVAL` | `30` | Seconds between checks |
| `COPY_TRADE_MIN_WIN_RATE` | `0.55` | Minimum win rate for a recommendation |
| `COPY_TRADE_MIN_VOLUME` | `10000` | Minimum volume ($) for a recommendation |

---

## Logs

All activity is written to `bot.log` in addition to the console.
