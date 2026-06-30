"""
Colossus — autonomous Polymarket sports trading bot.

Uses polymarket-us SDK for authenticated trading on Polymarket.US.
Scans markets every 60s, fires on 3+ triggers (T5 required), places up to $1 orders,
monitors positions for TP/SL every 1 min. Telegram alerts throughout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sys
from datetime import datetime, date, timezone, timedelta
from pathlib import Path

import aiohttp
import colorlog
import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from analyzer import evaluate_market
from polymarket_client import PolymarketClient
from position_manager import PositionManager
import scan_log

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
MAX_TRADES_SESSION = int(os.getenv("MAX_TRADES_SESSION", "10"))

_traded_this_session: set[str] = set()
_traded_events_this_session: set[str] = set()
_DAILY_COUNT_FILE = Path("daily_count.json")


def _load_daily_count() -> set[str]:
    """Load today's traded market IDs — resets automatically on new calendar day."""
    today = str(date.today())
    try:
        if _DAILY_COUNT_FILE.exists():
            data = json.loads(_DAILY_COUNT_FILE.read_text())
            if data.get("date") == today:
                return set(data.get("traded", []))
    except Exception:
        pass
    return set()


def _save_daily_count() -> None:
    try:
        _DAILY_COUNT_FILE.write_text(json.dumps({
            "date": str(date.today()),
            "traded": list(_traded_this_session),
        }))
    except Exception:
        pass


# ── Telegram helpers ──────────────────────────────────────────────────────────

async def _send(app: Application, text: str) -> None:
    try:
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT, text=text, parse_mode=None,
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
    reserve = position_mgr.get_reserve()
    tradeable = balance - reserve
    if tradeable < MIN_TRADE_USD:
        logger.info("Tradeable balance ($%.2f - $%.2f reserve = $%.2f) too low — skipping trades",
                    balance, reserve, tradeable)
        return

    markets = await client.get_sports_markets(limit=100)
    logger.info("Found %d eligible markets", len(markets))

    # Re-key any synced positions from slug to real CLOB token ID so price lookups work
    position_mgr.update_token_ids_from_markets(markets, client)

    if len(_traded_this_session) >= MAX_TRADES_SESSION:
        logger.info("Session trade cap reached (%d) — skipping scan", MAX_TRADES_SESSION)
        return

    signals_fired = 0

    for market in markets:
        if os.getenv("PAUSED", "false").lower() == "true":
            logger.info("Bot paused mid-scan — stopping")
            return

        mid = market.get("id") or market.get("conditionId") or ""
        if mid in _traded_this_session:
            continue

        # Only trade one sub-market per event (prevents Under 2.5 + KOR + Tie all firing)
        event_slug = market.get("eventSlug") or market.get("slug") or ""
        if event_slug and event_slug in _traded_events_this_session:
            continue

        # Skip if we already hold an open position in this market (persists across restarts)
        market_slug_check = client.get_market_slug(market)
        if position_mgr.has_position(market_slug_check):
            logger.debug("Already have position in %s — skipping", market_slug_check)
            scan_log.add(market_slug_check,
                         market.get("question") or market.get("title") or "",
                         None, "SKIP", "already holding position")
            continue

        try:
            signal = await evaluate_market(market, client)
        except Exception as exc:
            logger.debug("evaluate_market error: %s", exc)
            continue

        if signal is None:
            continue

        if os.getenv("PAUSED", "false").lower() == "true":
            logger.info("Bot paused before order — skipping")
            return

        if len(_traded_this_session) >= MAX_TRADES_SESSION:
            logger.info("Session trade cap reached (%d) — stopping scan", MAX_TRADES_SESSION)
            return

        balance = await client.get_balance()
        reserve = position_mgr.get_reserve()
        if balance - reserve < signal.amount_usd:
            logger.info("Trade would breach reserve — tradeable=$%.2f reserve=$%.2f trade=$%.2f — stopping",
                        balance - reserve, reserve, signal.amount_usd)
            break

        signals_fired += 1
        _traded_this_session.add(mid)
        if event_slug:
            _traded_events_this_session.add(event_slug)
        _save_daily_count()

        resp = await client.place_market_order(
            signal.market_slug, signal.side, signal.price_now, signal.amount_usd
        )

        logger.info("Order raw response: %s", resp)
        order_status = (resp or {}).get("status", "") if isinstance(resp, dict) else ""
        filled = order_status in ("matched", "filled", "MATCHED", "FILLED", "open", "OPEN") or (resp and not isinstance(resp, dict))
        scan_log.add(signal.market_slug, signal.question, signal.price_now, "ORDER",
                     f"{'filled' if filled else 'not filled'} — {signal.side} ${signal.amount_usd:.2f}",
                     signal.triggers, signal.conviction)

        if filled:
            msg = (
                f"🏆 Trade Opened\n"
                f"{signal.question[:80]}\n\n"
                f"Side: {signal.side} @ {signal.price_now:.3f}\n"
                f"Amount: ${signal.amount_usd:.2f} ({signal.conviction} conviction)\n"
                f"TP: {signal.tp_pct:.0%}  SL: {signal.sl_pct:.0%}\n"
                f"Event date: {signal.event_date}\n"
                f"Triggers: {', '.join(signal.triggers)}\n"
                f"Score: {signal.score}/100"
            )
            await _send(app, msg)
            position_mgr.record_entry(
                signal.token_id, signal.market_slug, signal.side,
                signal.price_now, signal.tp_pct, signal.sl_pct,
                amount_usd=signal.amount_usd,
                conviction=signal.conviction,
                triggers=signal.triggers,
            )
        else:
            status = order_status or "no response"
            msg = (
                f"⚠️ Signal fired — order not filled\n"
                f"{signal.question[:80]}\n\n"
                f"Side: {signal.side} @ {signal.price_now:.3f}\n"
                f"Triggers: {', '.join(signal.triggers)}\n"
                f"Status: {status}"
            )
            await _send(app, msg)

    if signals_fired == 0:
        logger.info("No signals this scan")


# ── Telegram commands ─────────────────────────────────────────────────────────

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    pos_mgr: PositionManager = ctx.bot_data["pos_mgr"]
    client: PolymarketClient = ctx.bot_data["client"]
    now      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    balance  = await client.get_balance()
    reserve  = pos_mgr.get_reserve()
    text = (
        f"Colossus Status\n"
        f"Time: {now}\n"
        f"Paused: {'yes' if os.getenv('PAUSED','false').lower()=='true' else 'no'}\n"
        f"Balance: ${balance:.2f}  Reserve: ${reserve:.2f}  Tradeable: ${balance - reserve:.2f}\n"
        f"Trade range: ${MIN_TRADE_USD:.2f} - ${MAX_TRADE_USD:.2f}\n"
        f"TP: {TP_PCT:.0%}  SL: {SL_PCT:.0%}\n"
        f"Scan interval: {SCAN_INTERVAL}s\n"
        f"Traded markets this session: {len(_traded_this_session)}"
    )
    await update.message.reply_text(text)


async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    client: PolymarketClient = ctx.bot_data["client"]
    positions = await client.get_open_positions()
    if not positions:
        await update.message.reply_text("No open positions.")
        return
    lines = ["Open Positions\n"]
    for p in positions[:10]:
        slug = (p.get("marketSlug") or p.get("slug") or "")[:20]
        size = p.get("size") or p.get("quantity") or "?"
        avg  = p.get("avgPrice") or p.get("price") or "?"
        lines.append(f"  {slug}  size={size}  avg={avg}")
    await update.message.reply_text("\n".join(lines))


async def cmd_sellall(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    pos_mgr: PositionManager = ctx.bot_data["pos_mgr"]
    client: PolymarketClient = ctx.bot_data["client"]
    entries = list(pos_mgr._entries.items())
    if not entries:
        await update.message.reply_text("No tracked positions to sell.")
        return
    await update.message.reply_text(f"Closing {len(entries)} position(s)…")
    for token_id, entry in entries:
        current_price = await client.get_current_price(token_id) or entry.price
        await pos_mgr._close(token_id, entry, current_price, "MANUAL /sellall")
    await update.message.reply_text("All positions closed.")


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    os.environ["PAUSED"] = "true"
    await update.message.reply_text("⏸ Bot paused.")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    os.environ["PAUSED"] = "false"
    await update.message.reply_text("▶️ Bot resumed.")


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    pos_mgr: PositionManager = ctx.bot_data["pos_mgr"]
    report = await pos_mgr.get_report()
    await update.message.reply_text(report)


async def daily_report(app: Application, pos_mgr: PositionManager) -> None:
    report = await pos_mgr.get_report()
    await _send(app, report)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    logger.info("Colossus starting up…")

    global _traded_this_session
    _traded_this_session = _load_daily_count()
    if _traded_this_session:
        logger.info("Resumed daily trade count: %d trades today", len(_traded_this_session))

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
    app.add_handler(CommandHandler("sellall",   cmd_sellall))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scan_markets, "interval", seconds=SCAN_INTERVAL,
        args=[client, app, pos_mgr], id="scan",
    )
    scheduler.add_job(
        pos_mgr.check_positions, "interval", minutes=1,
        start_date=datetime.now(timezone.utc) + timedelta(seconds=35),
        id="positions",
    )
    scheduler.add_job(
        daily_report, "cron", hour=23, minute=55,
        args=[app, pos_mgr], id="daily_report",
    )
    scheduler.start()

    # Dashboard — runs in the same event loop on Railway's PORT
    dash_port = int(os.getenv("PORT", "8080"))
    dash_cfg  = uvicorn.Config("dashboard:app", host="0.0.0.0", port=dash_port, log_level="warning")
    asyncio.create_task(uvicorn.Server(dash_cfg).serve())
    logger.info("Dashboard starting on port %d", dash_port)

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    mode = "Auto-trading" if trading_enabled else "Signal-alert mode (no client)"
    await _send(app, (
        f"🤖 Colossus online\n"
        f"Mode: {mode}\n"
        f"Scanning every {SCAN_INTERVAL}s  TP {TP_PCT:.0%}  SL {SL_PCT:.0%}\n"
        f"Trade range: ${MIN_TRADE_USD:.2f}-${MAX_TRADE_USD:.2f}\n"
        "Commands: /status /positions /report /pause /resume /sellall"
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
