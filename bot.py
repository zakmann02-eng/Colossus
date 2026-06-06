"""
Colossus — autonomous Polymarket sports trading bot.

Uses polymarket-us SDK for authenticated trading on Polymarket.US.
Scans markets every 60s, fires on 1+ triggers, places up to $2 orders,
monitors positions for TP/SL every 5 min. Telegram alerts throughout.
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
TP_PCT        = float(os.getenv("TAKE_PROFIT_PCT", "10.0")) / 100
SL_PCT        = float(os.getenv("STOP_LOSS_PCT",   "10.0")) / 100
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
    try:
        await _scan_markets_inner(client, app, position_mgr)
    except Exception as exc:
        logger.error("scan_markets crashed: %s", exc, exc_info=True)


async def _scan_markets_inner(
    client: PolymarketClient,
    app: Application,
    position_mgr: PositionManager,
) -> None:
    if os.getenv("PAUSED", "false").lower() == "true":
        # Auto-resume if we were paused due to low funds and balance has recovered
        if os.getenv("PAUSED_REASON") == "low_funds":
            balance = await client.get_balance()
            available = max(0.0, balance - position_mgr.reserve_usd)
            if available >= MIN_TRADE_USD:
                os.environ["PAUSED"] = "false"
                os.environ.pop("PAUSED_REASON", None)
                await _send(app, f"▶️ *Colossus auto-resumed* — available balance restored (${available:.2f})")
                logger.info("Auto-resumed: available $%.2f", available)
            else:
                logger.info("Still paused — available funds $%.2f (reserve $%.2f)", available, position_mgr.reserve_usd)
                return
        else:
            logger.info("Bot paused — skipping scan")
            return

    logger.info("Scanning markets…")
    # Re-sync open positions before scanning so has_position() blocks re-trades after redeployments
    await position_mgr.sync_from_exchange()
    balance = await client.get_balance()
    available = max(0.0, balance - position_mgr.reserve_usd)
    if available < MIN_TRADE_USD:
        logger.warning(
            "Insufficient available balance ($%.2f; reserve $%.2f) — halting trades",
            available, position_mgr.reserve_usd,
        )
        await _send(app, (
            f"⛔ *Colossus halted — insufficient funds*\n"
            f"Balance: ${balance:.2f} · Reserve: ${position_mgr.reserve_usd:.2f}\n"
            f"Available: ${available:.2f} (minimum: ${MIN_TRADE_USD:.2f})\n"
            f"Watching for funds — will auto-resume when balance recovers.\n"
            f"Or deposit and the next scan will pick it up automatically."
        ))
        os.environ["PAUSED"] = "true"
        os.environ["PAUSED_REASON"] = "low_funds"
        return

    markets = await client.get_sports_markets(limit=50)
    logger.info("Found %d eligible markets", len(markets))

    # Sort by volume but cap at 5 markets per event to spread across sports
    markets.sort(key=lambda m: float(m.get("volume24hr") or m.get("volume24Hour") or 0), reverse=True)
    seen_events: dict[str, int] = {}
    capped: list[dict] = []
    for m in markets:
        event_key = m.get("eventSlug") or m.get("slug") or ""
        count = seen_events.get(event_key, 0)
        if count < 5:
            capped.append(m)
            seen_events[event_key] = count + 1
        if len(capped) >= 50:
            break
    markets = capped
    logger.info("Evaluating %d markets across %d events", len(markets), len(seen_events))

    signals_fired = 0

    for market in markets:
        mid = market.get("id") or market.get("conditionId") or ""
        if mid in _traded_this_session:
            continue

        await asyncio.sleep(0.5)  # 0.5s between markets — ~25 req/min, avoids IP ban
        try:
            signal = await evaluate_market(market, client)
        except Exception as exc:
            logger.debug("evaluate_market error: %s", exc)
            continue

        if signal is None:
            continue

        # Skip if we already have an open position on this market (survives redeployments)
        if position_mgr.has_position(signal.market_slug):
            logger.info("SKIP already-positioned: %s", signal.market_slug)
            continue

        balance = await client.get_balance()
        available = max(0.0, balance - position_mgr.reserve_usd)
        if available < signal.amount_usd:
            logger.info("Insufficient available ($%.2f) for $%.2f trade — skipping", available, signal.amount_usd)
            continue

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
    raw = await client.get_open_positions()
    try:
        positions = list(raw)[:10] if raw else []
    except Exception:
        positions = []
    if not positions:
        await update.message.reply_text("No open positions.")
        return
    lines = [f"*Open Positions* ({len(positions)})\n"]
    for p in positions:
        if not isinstance(p, dict):
            lines.append(f"• (non-dict entry: {str(p)[:60]})")
            continue
        slug = (p.get("marketSlug") or p.get("slug") or p.get("market") or p.get("name") or "unknown")[:25]
        size = p.get("size") or p.get("quantity") or p.get("shares") or "?"
        avg  = p.get("avgPrice") or p.get("price") or p.get("avgCost") or "?"
        curr = p.get("currentPrice") or p.get("marketPrice") or p.get("lastPrice") or "?"
        pnl  = p.get("percentPnl") or p.get("pnlPercent") or p.get("unrealizedPnlPercent") or "?"
        lines.append(f"• `{slug}`\n  size={size} avg={avg} now={curr} pnl={pnl}%")
    # If every position had no recognisable fields, show raw keys for diagnosis
    if len(lines) == 1 and positions and isinstance(positions[0], dict):
        lines.append(f"_(fields: {', '.join(positions[0].keys())})_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    os.environ["PAUSED"] = "true"
    await update.message.reply_text("⏸ Bot paused.")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    os.environ["PAUSED"] = "false"
    await update.message.reply_text("▶️ Bot resumed.")


async def cmd_sellall(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    pos_mgr: PositionManager = ctx.bot_data["pos_mgr"]
    await update.message.reply_text("🏳️ Closing all open positions…")
    count = await pos_mgr.sell_all()
    if count:
        await update.message.reply_text(f"Done — {count} position(s) closed.")
    else:
        await update.message.reply_text("No tracked positions to close.")


async def cmd_sellhalf(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    pos_mgr: PositionManager = ctx.bot_data["pos_mgr"]
    await update.message.reply_text("✂️ Selling half of each open position…")
    count = await pos_mgr.sell_half()
    if count:
        await update.message.reply_text(f"Done — halved {count} position(s).")
    else:
        await update.message.reply_text("No tracked positions to sell.")


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
    app.add_handler(CommandHandler("sellall",   cmd_sellall))
    app.add_handler(CommandHandler("sellhalf",  cmd_sellhalf))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scan_markets, "interval", seconds=SCAN_INTERVAL,
        args=[client, app, pos_mgr], id="scan",
    )
    scheduler.add_job(
        pos_mgr.check_positions, "interval", minutes=1, id="positions",
    )
    scheduler.add_job(
        pos_mgr.send_daily_report, "cron", hour=23, minute=55, id="daily_report",
    )
    scheduler.start()

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # Register any positions already open on the exchange so TP/SL fires on them
    await pos_mgr.sync_from_exchange()

    mode = "Auto-trading" if trading_enabled else "Signal-alert mode (no client)"
    await _send(app, (
        f"🤖 *Colossus online*\n"
        f"Mode: {mode}\n"
        f"Scanning every {SCAN_INTERVAL}s · TP {TP_PCT:.0%} · SL {SL_PCT:.0%}\n"
        f"Trade range: ${MIN_TRADE_USD:.2f}–${MAX_TRADE_USD:.2f}\n"
        "Commands: /status /positions /pause /resume /sellall /sellhalf"
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
