"""
Colossus — Polymarket sports trade signal bot.

Scans sports markets every 60s, fires on 2+ triggers, sends Telegram alert
with a direct link so you can execute on the Polymarket app.

No private key required — works with email/Magic.link login.
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
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from analyzer import evaluate_market
from polymarket_client import PolymarketClient

load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    handler = colorlog.StreamHandler()
    handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s %(levelname)-8s%(reset)s %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG":    "cyan",
            "INFO":     "green",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        },
    ))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

_setup_logging()
logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────

def _require(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        logger.critical("Missing required env var: %s", name)
        sys.exit(1)
    return val

TELEGRAM_TOKEN = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT  = int(_require("TELEGRAM_CHAT_ID"))

MAX_TRADE_USD  = float(os.getenv("MAX_TRADE_USD",  "2.00"))
SCAN_INTERVAL  = int(os.getenv("SCAN_INTERVAL",    "60"))

# Deduplicate — don't re-alert the same market within a session
_alerted_this_session: set[str] = set()


# ── Telegram helpers ──────────────────────────────────────────────────────────

async def _send(app: Application, text: str) -> None:
    try:
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT, text=text, parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.error("Telegram send failed: %s", exc)


# ── Core scan job ─────────────────────────────────────────────────────────────

async def scan_markets(client: PolymarketClient, app: Application) -> None:
    if os.getenv("PAUSED", "false").lower() == "true":
        logger.info("Bot paused — skipping scan")
        return

    logger.info("Scanning sports markets…")
    markets = await client.get_sports_markets(limit=200)
    logger.info("Found %d eligible markets", len(markets))

    signals_fired = 0

    for market in markets:
        mid = market.get("id") or market.get("conditionId") or ""
        if mid in _alerted_this_session:
            continue

        try:
            signal = await evaluate_market(market, client)
        except Exception as exc:
            logger.debug("evaluate_market error: %s", exc)
            continue

        if signal is None:
            continue

        signals_fired += 1
        _alerted_this_session.add(mid)

        url = client.market_url(market)

        logger.info(
            "SIGNAL: %s | %s @ %.2f | triggers=%s | score=%d",
            signal.question[:60], signal.side, signal.price_now,
            signal.triggers, signal.score,
        )

        msg = (
            f"🏆 *Trade Signal*\n"
            f"*{signal.question[:80]}*\n\n"
            f"Recommended: *{signal.side}*\n"
            f"Current price: {signal.price_now:.3f} ({signal.price_now*100:.1f}¢)\n"
            f"Suggested amount: ${MAX_TRADE_USD:.2f}\n"
            f"Event date: {signal.event_date}\n\n"
            f"Triggers: {', '.join(signal.triggers)}\n"
            f"Confidence: {signal.score}/100\n\n"
            f"👉 [Open on Polymarket]({url})"
        )
        await _send(app, msg)

    if signals_fired == 0:
        logger.info("No signals this scan")


# ── Telegram commands ─────────────────────────────────────────────────────────

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    text = (
        f"*Colossus Status*\n"
        f"Time: {now}\n"
        f"Paused: {'yes' if os.getenv('PAUSED','false').lower()=='true' else 'no'}\n"
        f"Suggested trade size: ${MAX_TRADE_USD:.2f}\n"
        f"Scan interval: {SCAN_INTERVAL}s\n"
        f"Signals sent this session: {len(_alerted_this_session)}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    os.environ["PAUSED"] = "true"
    await update.message.reply_text("⏸ Bot paused.")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    os.environ["PAUSED"] = "false"
    await update.message.reply_text("▶️ Bot resumed.")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    logger.info("Colossus starting up…")

    session = aiohttp.ClientSession()
    client  = PolymarketClient(session)
    app     = Application.builder().token(TELEGRAM_TOKEN).build()

    app.bot_data["client"] = client

    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pause",  cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scan_markets,
        "interval",
        seconds = SCAN_INTERVAL,
        args    = [client, app],
        id      = "scan",
    )
    scheduler.start()

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    await _send(app, (
        "🤖 *Colossus online*\n"
        f"Scanning sports markets every {SCAN_INTERVAL}s\n"
        f"Suggested trade size: ${MAX_TRADE_USD:.2f}\n\n"
        "When a signal fires you'll get a direct link to execute on Polymarket.\n"
        "Commands: /status /pause /resume"
    ))

    logger.info("Bot running. Press Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        logger.info("Shutting down…")
        scheduler.shutdown(wait=False)
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
