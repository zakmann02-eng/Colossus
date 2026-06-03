"""
Colossus — autonomous Polymarket sports trading bot.

Uses API Key + Secret from polymarket.us/developer — no private key needed.
Scans sports markets every 60s, fires on 2+ triggers, places up to $2 orders,
monitors positions for TP/SL every 30 min. Telegram alerts throughout.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
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
from position_manager import PositionManager

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

TELEGRAM_TOKEN   = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT    = int(_require("TELEGRAM_CHAT_ID"))
POLY_API_KEY     = _require("POLYMARKET_API_KEY")
POLY_API_SECRET  = _require("POLYMARKET_API_SECRET")
POLY_PASSPHRASE  = os.getenv("POLYMARKET_API_PASSPHRASE", "").strip()

MIN_TRADE_USD  = float(os.getenv("MIN_TRADE_USD",   "0.10"))
MAX_TRADE_USD  = float(os.getenv("MAX_TRADE_USD",   "2.00"))
TP_PCT         = float(os.getenv("TAKE_PROFIT_PCT", "10.0")) / 100
SL_PCT         = float(os.getenv("STOP_LOSS_PCT",   "10.0")) / 100
SCAN_INTERVAL  = int(os.getenv("SCAN_INTERVAL",     "60"))


def _trade_amount() -> float:
    return round(random.uniform(MIN_TRADE_USD, MAX_TRADE_USD), 2)

# Deduplicate — don't re-enter the same market within a session
_traded_this_session: set[str] = set()


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

async def scan_markets(
    client: PolymarketClient,
    app: Application,
    position_mgr: PositionManager,
) -> None:
    if os.getenv("PAUSED", "false").lower() == "true":
        logger.info("Bot paused — skipping scan")
        return

    logger.info("Scanning sports markets…")
    balance = await client.get_balance()
    if balance < MIN_TRADE_USD:
        logger.info("Insufficient balance ($%.2f) — skipping trades", balance)
        return

    markets = await client.get_sports_markets(limit=200)
    logger.info("Found %d eligible markets", len(markets))

    signals_fired = 0

    for market in markets:
        mid = market.get("id") or market.get("conditionId") or ""
        if mid in _traded_this_session:
            continue

        try:
            signal = await evaluate_market(market, client)
        except Exception as exc:
            logger.debug("evaluate_market error: %s", exc)
            continue

        if signal is None:
            continue

        signals_fired += 1
        _traded_this_session.add(mid)

        resp = await client.place_market_order(
            signal.token_id, "BUY", signal.amount_usd
        )

        status = "✅ filled" if resp else "⚠️ failed"
        msg = (
            f"🏆 *Trade Opened*\n"
            f"*{signal.question[:80]}*\n\n"
            f"Side: `{signal.side}` @ {signal.price_now:.3f}\n"
            f"Amount: ${signal.amount_usd:.2f} ({signal.conviction} conviction)\n"
            f"TP: {signal.tp_pct:.0%} · SL: {signal.sl_pct:.0%}\n"
            f"Event date: {signal.event_date}\n"
            f"Triggers: {', '.join(signal.triggers)}\n"
            f"Score: {signal.score}/100\n"
            f"Order: {status}"
        )
        await _send(app, msg)

        if resp:
            position_mgr.record_entry(
                signal.token_id, signal.price_now,
                signal.tp_pct, signal.sl_pct
            )

    if signals_fired == 0:
        logger.info("No signals this scan")


# ── Telegram commands ─────────────────────────────────────────────────────────

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    text = (
        f"*Colossus Status*\n"
        f"Time: {now}\n"
        f"Paused: {'yes' if os.getenv('PAUSED','false').lower()=='true' else 'no'}\n"
        f"Trade range: ${MIN_TRADE_USD:.2f} – ${MAX_TRADE_USD:.2f}\n"
        f"TP: {TP_PCT:.0%} | SL: {SL_PCT:.0%}\n"
        f"Scan interval: {SCAN_INTERVAL}s\n"
        f"Traded markets this session: {len(_traded_this_session)}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    client: PolymarketClient = ctx.bot_data["client"]
    positions = await client.get_open_positions()
    if not positions:
        await update.message.reply_text("No open positions.")
        return
    lines = ["*Open Positions*\n"]
    for p in positions[:10]:
        tid  = (p.get("asset") or p.get("tokenId") or "")[:12]
        size = p.get("size") or p.get("amount") or "?"
        avg  = p.get("avgPrice") or p.get("average_price") or "?"
        lines.append(f"• `{tid}…` size={size} avg={avg}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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
    client = PolymarketClient(session, POLY_API_KEY, POLY_API_SECRET, POLY_PASSPHRASE)
    trading_enabled = client._clob_client is not None

    app     = Application.builder().token(TELEGRAM_TOKEN).build()
    pos_mgr = PositionManager(client, app, TELEGRAM_CHAT, TP_PCT, SL_PCT)

    app.bot_data["client"] = client

    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("pause",     cmd_pause))
    app.add_handler(CommandHandler("resume",    cmd_resume))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scan_markets,
        "interval",
        seconds = SCAN_INTERVAL,
        args    = [client, app, pos_mgr],
        id      = "scan",
    )
    scheduler.add_job(
        pos_mgr.check_positions,
        "interval",
        minutes = 5,
        id      = "positions",
    )
    scheduler.start()

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    mode = "Auto-trading" if trading_enabled else "Signal-alert mode (no private key)"
    await _send(app, (
        f"🤖 *Colossus online*\n"
        f"Mode: {mode}\n"
        f"Scanning every {SCAN_INTERVAL}s · TP {TP_PCT:.0%} · SL {SL_PCT:.0%}\n"
        f"Trade range: ${MIN_TRADE_USD:.2f}–${MAX_TRADE_USD:.2f}\n"
        "Commands: /status /positions /pause /resume"
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
