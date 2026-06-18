"""
Colossus — autonomous Polymarket sports trading bot.

Uses polymarket-us SDK for authenticated trading on Polymarket.US.
Scans markets every 60s, fires on 2+ triggers, places up to $1 orders,
monitors positions for TP/SL every 1 min. Telegram alerts throughout.
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

TELEGRAM_TOKEN  = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT   = int(_require("TELEGRAM_CHAT_ID"))
POLY_KEY_ID     = _require("POLYMARKET_KEY_ID")
POLY_SECRET_KEY = _require("POLYMARKET_SECRET_KEY")

MIN_TRADE_USD = float(os.getenv("MIN_TRADE_USD",   "0.10"))
MAX_TRADE_USD = float(os.getenv("MAX_TRADE_USD",   "1.00"))
TP_PCT        = float(os.getenv("TAKE_PROFIT_PCT", "20.0")) / 100
SL_PCT        = float(os.getenv("STOP_LOSS_PCT",   "8.0"))  / 100
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL",     "60"))

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

    logger.info("Scanning markets…")
    balance = await client.get_balance()
    if balance < MIN_TRADE_USD:
        logger.info("Insufficient balance ($%.2f) — skipping trades", balance)
        return

    markets = await client.get_sports_markets(limit=100)
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

        balance = await client.get_balance()
        if balance < signal.amount_usd:
            logger.info("Insufficient balance ($%.2f) for $%.2f trade — stopping", balance, signal.amount_usd)
            break

        signals_fired += 1
        _traded_this_session.add(mid)

        resp = await client.place_market_order(
            signal.market_slug, signal.side, signal.price_now, signal.amount_usd
        )

        logger.info("Order raw response: %s", resp)
        order_status = (resp or {}).get("status", "") if isinstance(resp, dict) else ""
        filled = order_status in ("matched", "filled", "MATCHED", "FILLED", "open", "OPEN") or (resp and not isinstance(resp, dict))
        status = "✅ filled" if filled else f"⚠️ not filled ({order_status or 'no response'})"

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

        if filled:
            position_mgr.record_entry(
                signal.token_id, signal.market_slug, signal.side,
                signal.price_now, signal.tp_pct, signal.sl_pct,
                amount_usd=signal.amount_usd,
                conviction=signal.conviction,
                triggers=signal.triggers,
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
        slug = (p.get("marketSlug") or p.get("slug") or "")[:20]
        size = p.get("size") or p.get("quantity") or "?"
        avg  = p.get("avgPrice") or p.get("price") or "?"
        lines.append(f"• `{slug}` size={size} avg={avg}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    os.environ["PAUSED"] = "true"
    await update.message.reply_text("⏸ Bot paused.")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    os.environ["PAUSED"] = "false"
    await update.message.reply_text("▶️ Bot resumed.")


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    pos_mgr: PositionManager = ctx.bot_data["pos_mgr"]
    report = await pos_mgr.get_report()
    await update.message.reply_text(report, parse_mode="Markdown")


async def daily_report(app: Application, pos_mgr: PositionManager) -> None:
    report = await pos_mgr.get_report()
    await _send(app, report)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    logger.info("Colossus starting up…")

    session = aiohttp.ClientSession()
    client  = PolymarketClient(session, POLY_KEY_ID, POLY_SECRET_KEY)
    trading_enabled = client._us_client is not None

    app     = Application.builder().token(TELEGRAM_TOKEN).build()
    pos_mgr = PositionManager(client, app, TELEGRAM_CHAT, TP_PCT, SL_PCT)

    app.bot_data["client"]  = client
    app.bot_data["pos_mgr"] = pos_mgr

    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("pause",     cmd_pause))
    app.add_handler(CommandHandler("resume",    cmd_resume))
    app.add_handler(CommandHandler("report",    cmd_report))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scan_markets, "interval", seconds=SCAN_INTERVAL,
        args=[client, app, pos_mgr], id="scan",
    )
    scheduler.add_job(
        pos_mgr.check_positions, "interval", minutes=1, id="positions",
    )
    scheduler.add_job(
        daily_report, "cron", hour=23, minute=55,
        args=[app, pos_mgr], id="daily_report",
    )
    scheduler.start()

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    mode = "Auto-trading" if trading_enabled else "Signal-alert mode (no client)"
    await _send(app, (
        f"🤖 *Colossus online*\n"
        f"Mode: {mode}\n"
        f"Scanning every {SCAN_INTERVAL}s · TP {TP_PCT:.0%} · SL {SL_PCT:.0%}\n"
        f"Trade range: ${MIN_TRADE_USD:.2f}–${MAX_TRADE_USD:.2f}\n"
        "Commands: /status /positions /report /pause /resume"
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
