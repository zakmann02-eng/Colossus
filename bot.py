"""
Colossus Polymarket Wallet Watcher
------------------------------------
Tracks a configured wallet's trades on Polymarket.
Sends Telegram alerts with trade details and analysis.

Usage:
    python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

import aiohttp
import colorlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError

from polymarket_client import PolymarketClient
from trade_analyzer import TradeAlert, parse_trade

load_dotenv()

# ------------------------------------------------------------------ #
# Logging                                                             #
# ------------------------------------------------------------------ #

handler = colorlog.StreamHandler()
handler.setFormatter(
    colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s]%(reset)s %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "bold_red",
        },
    )
)
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Config                                                              #
# ------------------------------------------------------------------ #

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
TRACKED_WALLET: str = os.getenv("TRACKED_WALLET", "").lower()
WALLET_LABEL: str = os.getenv("WALLET_LABEL", "Tracked Wallet")
POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "30"))
MIN_TRADE_SIZE_USD: float = float(os.getenv("MIN_TRADE_SIZE_USD", "10"))

# ------------------------------------------------------------------ #
# State                                                               #
# ------------------------------------------------------------------ #

seen_trade_ids: set[str] = set()
market_cache: dict[str, dict] = {}


# ------------------------------------------------------------------ #
# Telegram helpers                                                    #
# ------------------------------------------------------------------ #

async def send_telegram(bot: Bot, text: str) -> None:
    chunks = _split_message(text)
    for chunk in chunks:
        try:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=chunk,
                parse_mode="Markdown",
            )
            logger.info("Telegram message sent (%d chars)", len(chunk))
        except TelegramError:
            # Retry without markdown formatting
            try:
                cleaned = chunk.replace("*", "").replace("`", "").replace("_", "")
                await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=cleaned)
            except TelegramError as exc:
                logger.error("Telegram send error: %s", exc)
        await asyncio.sleep(0.35)


def _split_message(text: str, limit: int = 4096) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current, length = [], [], 0
    for line in text.splitlines(keepends=True):
        if length + len(line) > limit:
            chunks.append("".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


async def send_startup_message(bot: Bot) -> None:
    msg = (
        "🤖 *Colossus Bot — Online*\n\n"
        f"👛 Tracking: `{WALLET_LABEL}`\n"
        f"📍 `{TRACKED_WALLET[:8]}...{TRACKED_WALLET[-6:]}`\n"
        f"⏱️ Poll interval: {POLL_INTERVAL}s\n"
        f"💵 Min trade size: ${MIN_TRADE_SIZE_USD}\n\n"
        "Alerts include:\n"
        "• Entry and exit detection\n"
        "• Event date\n"
        "• Market trend & success probability\n"
        "• P&L on exits\n\n"
        "_Bot is live. Watching the markets…_"
    )
    await send_telegram(bot, msg)


# ------------------------------------------------------------------ #
# Market cache                                                        #
# ------------------------------------------------------------------ #

async def resolve_market(client: PolymarketClient, trade: dict) -> dict | None:
    token_id = trade.get("asset_id") or trade.get("assetId") or trade.get("tokenId") or ""
    condition_id = trade.get("conditionId") or trade.get("condition_id") or ""

    if condition_id and condition_id in market_cache:
        return market_cache[condition_id]
    if token_id and token_id in market_cache:
        return market_cache[token_id]

    market = None
    if token_id:
        market = await client.get_market_by_token(token_id)
    if not market and condition_id:
        market = await client.get_market(condition_id)

    if market:
        key = condition_id or token_id
        market_cache[key] = market

    return market


# ------------------------------------------------------------------ #
# Tracked wallet watcher                                              #
# ------------------------------------------------------------------ #

async def check_tracked_wallet(client: PolymarketClient, bot: Bot) -> None:
    if os.getenv("PAUSED", "").lower() in ("1", "true", "yes"):
        logger.info("Bot is PAUSED — skipping wallet check")
        return

    logger.info("Checking wallet %s…", TRACKED_WALLET[:10])
    trades = await client.get_wallet_trades(TRACKED_WALLET, limit=20)

    new_trades: list[dict] = []
    for trade in trades:
        tid = _trade_id(trade)
        if tid not in seen_trade_ids:
            seen_trade_ids.add(tid)
            new_trades.append(trade)

    if not new_trades:
        logger.debug("No new trades.")
        return

    logger.info("Found %d new trade(s).", len(new_trades))

    for trade in reversed(new_trades):
        # Today only (UTC)
        ts = _get_timestamp(trade)
        if ts:
            trade_date = datetime.fromtimestamp(ts, tz=timezone.utc).date()
            if trade_date < datetime.now(timezone.utc).date():
                logger.debug("Skipping trade from %s — not today", trade_date)
                continue

        size = _get_size(trade)
        if size < MIN_TRADE_SIZE_USD:
            logger.debug("Skipping small trade $%.2f", size)
            continue

        market = await resolve_market(client, trade)

        # Skip resolved markets
        if market and not client.is_market_active(market):
            logger.info("Market resolved — skipping")
            continue

        # Skip US-restricted markets
        if market and not client.is_market_us_accessible(market):
            logger.info("Market US-restricted — skipping")
            continue

        event_date = client.get_event_date(market) if market else "N/A"
        trend = await client.get_market_trend(market, trade) if market else None

        alert: TradeAlert = parse_trade(
            trade=trade,
            wallet=TRACKED_WALLET,
            label=WALLET_LABEL,
            market=market,
            event_date=event_date,
            trend=trend,
        )

        if alert.should_suppress():
            logger.info("Trade score %d/100 — below threshold, suppressed", alert.score)
            continue

        await send_telegram(bot, alert.format_telegram())
        await asyncio.sleep(0.5)


async def check_positions(client: PolymarketClient, bot: Bot) -> None:
    if os.getenv("PAUSED", "").lower() in ("1", "true", "yes"):
        return

    logger.info("Fetching open positions…")
    positions = await client.get_wallet_positions(TRACKED_WALLET)
    if not positions:
        logger.info("No open positions found.")
        return

    lines = [f"📋 *Open Positions — {WALLET_LABEL}*\n"]
    total_value = 0.0

    for pos in positions[:20]:
        size = _pos_size(pos)
        price = _pos_price(pos)
        outcome = pos.get("outcome") or pos.get("outcomeName") or "?"
        title = pos.get("title") or pos.get("question") or pos.get("market") or "Unknown Market"
        total_value += size
        lines.append(
            f"• *{outcome}* — {title[:60]}\n"
            f"  💰 ${size:,.2f} @ {price * 100:.1f}¢"
        )

    lines.append(f"\n📊 *Total open value: ${total_value:,.2f}*")
    await send_telegram(bot, "\n".join(lines))


# ------------------------------------------------------------------ #
# Utility                                                             #
# ------------------------------------------------------------------ #

def _trade_id(trade: dict) -> str:
    return (
        trade.get("id")
        or trade.get("transactionHash")
        or trade.get("txHash")
        or str(trade)
    )


def _get_size(trade: dict) -> float:
    for key in ("usdcSize", "size", "amount", "value", "tradeAmount"):
        v = trade.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def _get_timestamp(trade: dict) -> float | None:
    for key in ("timestamp", "createdAt", "created_at", "time"):
        v = trade.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def _pos_size(pos: dict) -> float:
    for key in ("value", "usdcValue", "size", "currentValue", "cashBalance"):
        v = pos.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def _pos_price(pos: dict) -> float:
    for key in ("curPrice", "currentPrice", "price", "avgPrice"):
        v = pos.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


# ------------------------------------------------------------------ #
# Scheduler setup                                                     #
# ------------------------------------------------------------------ #

async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN not set.")
        sys.exit(1)
    if not TELEGRAM_CHAT_ID:
        logger.critical("TELEGRAM_CHAT_ID not set.")
        sys.exit(1)
    if not TRACKED_WALLET:
        logger.critical("TRACKED_WALLET not set.")
        sys.exit(1)

    logger.info("Starting Colossus Bot…")
    logger.info("Tracked wallet: %s (%s)", WALLET_LABEL, TRACKED_WALLET)

    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        client = PolymarketClient(session)
        bot = Bot(token=TELEGRAM_BOT_TOKEN)

        # Prime seen_trade_ids so we don't spam old trades on startup
        logger.info("Priming trade history…")
        existing = await client.get_wallet_trades(TRACKED_WALLET, limit=50)
        for t in existing:
            seen_trade_ids.add(_trade_id(t))
        logger.info("Primed %d existing trade IDs.", len(seen_trade_ids))

        await send_startup_message(bot)

        scheduler = AsyncIOScheduler()

        # Main wallet watcher: every POLL_INTERVAL seconds
        scheduler.add_job(
            check_tracked_wallet,
            "interval",
            seconds=POLL_INTERVAL,
            args=[client, bot],
            id="wallet_watcher",
            next_run_time=datetime.now(tz=timezone.utc),
        )

        # Position summary: every 30 minutes
        scheduler.add_job(
            check_positions,
            "interval",
            minutes=30,
            args=[client, bot],
            id="position_summary",
        )

        scheduler.start()
        logger.info("Scheduler started. Bot is running. Press Ctrl+C to stop.")

        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutting down…")
            scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
