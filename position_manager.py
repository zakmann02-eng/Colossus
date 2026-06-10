from __future__ import annotations

import csv
import json
import logging
import os
from dataclasses import asdict, dataclass, fields as dc_fields
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polymarket_client import PolymarketClient
    from telegram.ext import Application

logger = logging.getLogger(__name__)

_PERSIST_FILE = os.path.join(os.path.dirname(__file__), "positions.json")
_RESERVE_FILE = os.path.join(os.path.dirname(__file__), "reserve.json")
_TRADE_LOG    = os.path.join(os.path.dirname(__file__), "trade_log.csv")

PROFIT_RESERVE_PCT = 0.30  # 30% of each TP profit locked away

_PRICE_MISS_LIMIT = 5  # force-close after this many consecutive price misses

_CSV_HEADERS = [
    "timestamp", "market_slug", "side", "conviction",
    "entry_price", "exit_price", "pnl_pct", "pnl_usd",
    "outcome", "amount_usd", "triggers",
]


@dataclass
class _Entry:
    market_slug:  str
    side:         str
    price:        float
    tp:           float
    sl:           float
    amount_usd:   float = 0.50
    price_misses: int   = 0
    conviction:   str   = ""
    triggers:     str   = ""


class PositionManager:
    def __init__(
        self,
        client: "PolymarketClient",
        app: "Application",
        chat_id: int,
        default_tp: float,
        default_sl: float,
    ) -> None:
        self._client     = client
        self._app        = app
        self._chat_id    = chat_id
        self._default_tp = default_tp
        self._default_sl = default_sl
        self._entries: dict[str, _Entry] = {}

        self._reserve_usd: float = 0.0

        self._day_opened:   int   = 0
        self._day_tp:       int   = 0
        self._day_sl:       int   = 0
        self._day_pnl:      float = 0.0
        self._day_reserved: float = 0.0

        self._load()
        self._load_reserve()
        self._init_trade_log()

    def _load(self) -> None:
        try:
            with open(_PERSIST_FILE) as f:
                data = json.load(f)
            valid = {f.name for f in dc_fields(_Entry)}
            for token_id, d in data.items():
                self._entries[token_id] = _Entry(**{k: v for k, v in d.items() if k in valid})
            if self._entries:
                logger.info("Loaded %d persisted positions from disk", len(self._entries))
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("Failed to load positions.json: %s", exc)

    def _save(self) -> None:
        try:
            with open(_PERSIST_FILE, "w") as f:
                json.dump({k: asdict(v) for k, v in self._entries.items()}, f)
        except Exception as exc:
            logger.warning("Failed to save positions.json: %s", exc)

    def _load_reserve(self) -> None:
        try:
            with open(_RESERVE_FILE) as f:
                data = json.load(f)
            self._reserve_usd = float(data.get("reserve_usd", 0.0))
            logger.info("Loaded profit reserve: $%.2f", self._reserve_usd)
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("Failed to load reserve.json: %s", exc)

    def _save_reserve(self) -> None:
        try:
            with open(_RESERVE_FILE, "w") as f:
                json.dump({"reserve_usd": round(self._reserve_usd, 4)}, f)
        except Exception as exc:
            logger.warning("Failed to save reserve.json: %s", exc)

    def _init_trade_log(self) -> None:
        if not os.path.exists(_TRADE_LOG):
            try:
                with open(_TRADE_LOG, "w", newline="") as f:
                    csv.DictWriter(f, fieldnames=_CSV_HEADERS).writeheader()
                logger.info("Created trade log: %s", _TRADE_LOG)
            except Exception as exc:
                logger.warning("Could not create trade log: %s", exc)

    def _log_trade(
        self,
        entry: _Entry,
        exit_price: float,
        pnl_pct: float,
        pnl_usd: float,
        outcome: str,
    ) -> None:
        try:
            with open(_TRADE_LOG, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=_CSV_HEADERS)
                writer.writerow({
                    "timestamp":   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "market_slug": entry.market_slug,
                    "side":        entry.side,
                    "conviction":  entry.conviction,
                    "entry_price": round(entry.price, 4),
                    "exit_price":  round(exit_price, 4),
                    "pnl_pct":     round(pnl_pct * 100, 2),
                    "pnl_usd":     round(pnl_usd, 4),
                    "outcome":     outcome,
                    "amount_usd":  round(entry.amount_usd, 2),
                    "triggers":    entry.triggers,
                })
        except Exception as exc:
            logger.warning("Failed to write trade log: %s", exc)

    @property
    def reserve_usd(self) -> float:
        return self._reserve_usd

    def record_trade_opened(self) -> None:
        self._day_opened += 1

    async def send_daily_report(self) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        total_closed = self._day_tp + self._day_sl
        win_rate = f"{self._day_tp / total_closed:.0%}" if total_closed > 0 else "N/A"
        pnl_sign = "+" if self._day_pnl >= 0 else ""
        balance = await self._client.get_balance()

        msg = (
            f"📊 *Colossus Daily Report — {now}*\n\n"
            f"Trades opened:  {self._day_opened}\n"
            f"Closed TP ✅:   {self._day_tp}\n"
            f"Closed SL 🔴:   {self._day_sl}\n"
            f"Win rate:        {win_rate}\n\n"
            f"Day P&L:         {pnl_sign}${self._day_pnl:.2f}\n"
            f"Reserved today:  ${self._day_reserved:.2f}\n"
            f"Total reserve:   ${self._reserve_usd:.2f}\n\n"
            f"Account balance: ${balance:.2f}\n"
            f"Available:       ${max(0.0, balance - self._reserve_usd):.2f}"
        )
        try:
            await self._app.bot.send_message(
                chat_id=self._chat_id, text=msg, parse_mode="Markdown",
            )
        except Exception as exc:
            logger.error("Failed to send daily report: %s", exc)

        self._day_opened   = 0
        self._day_tp       = 0
        self._day_sl       = 0
        self._day_pnl      = 0.0
        self._day_reserved = 0.0
        logger.info("Daily report sent and counters reset")

    async def sync_from_exchange(self) -> None:
        try:
            live = list(await self._client.get_open_positions() or [])
        except Exception as exc:
            logger.warning("sync_from_exchange: get_open_positions failed: %s", exc)
            return

        tracked_slugs = {e.market_slug for e in self._entries.values()}
        added = 0
        for p in live:
            if not isinstance(p, dict):
                continue
            meta = p.get("marketMetadata") or {}
            slug = meta.get("slug") or p.get("marketSlug") or p.get("slug") or ""
            if not slug or slug in tracked_slugs:
                continue
            intent = str(p.get("intent") or p.get("side") or p.get("positionType") or "").upper()
            if "SHORT" in intent or "NO" in intent:
                side = "NO"
            elif "LONG" in intent or "YES" in intent:
                side = "YES"
            else:
                net = float(p.get("netPosition") or p.get("netPositionDecimal") or 0)
                side = "NO" if net < 0 else "YES"
            avg_px = p.get("avgPx") or p.get("avgPrice") or p.get("price") or {}
            price = float(avg_px.get("value") if isinstance(avg_px, dict) else avg_px or 0.50) or 0.50
            cash = p.get("cashValue") or p.get("value") or p.get("size") or {}
            size_usd = float(cash.get("value") if isinstance(cash, dict) else cash or 0)
            if price <= 0 or size_usd <= 0:
                continue
            token_id = f"sync_{slug}_{side}"
            self._entries[token_id] = _Entry(
                market_slug = slug,
                side        = side,
                price       = price,
                tp          = self._default_tp,
                sl          = self._default_sl,
                amount_usd  = size_usd,
            )
            tracked_slugs.add(slug)
            added += 1

        if added:
            self._save()
            logger.info("Synced %d live position(s) from exchange into TP/SL tracker", added)
        else:
            logger.info("sync_from_exchange: no new positions to register")
        if live:
            logger.info("Raw portfolio position sample: %s", live[0])

    def record_entry(
        self,
        token_id:    str,
        market_slug: str,
        side:        str,
        entry_price: float,
        tp_pct:      float | None = None,
        sl_pct:      float | None = None,
        amount_usd:  float = 0.50,
        conviction:  str   = "",
        triggers:    list  | None = None,
    ) -> None:
        self._entries[token_id] = _Entry(
            market_slug = market_slug,
            side        = side,
            price       = entry_price,
            tp          = tp_pct if tp_pct is not None else self._default_tp,
            sl          = sl_pct if sl_pct is not None else self._default_sl,
            amount_usd  = amount_usd,
            conviction  = conviction,
            triggers    = ",".join(triggers) if triggers else "",
        )
        self._save()
        self._day_opened += 1
        logger.info(
            "Recorded entry: slug=%s side=%s price=%.3f TP=%.0f%% SL=%.0f%% conviction=%s",
            market_slug, side, entry_price,
            (tp_pct or self._default_tp) * 100,
            (sl_pct or self._default_sl) * 100,
            conviction,
        )

    def has_position(self, market_slug: str) -> bool:
        return any(e.market_slug == market_slug for e in self._entries.values())

    async def _get_price_for_entry(self, token_id: str, entry: "_Entry") -> float | None:
        tid = str(token_id)
        is_clob_token = (
            not tid.startswith("sync_")
            and len(tid) >= 32
            and tid.replace("-", "").isalnum()
        )
        if is_clob_token:
            price = await self._client.get_current_price(token_id)
            if price is not None:
                return price

        try:
            positions = await self._client.get_open_positions()
            for p in (positions or []):
                if not isinstance(p, dict):
                    continue
                meta = p.get("marketMetadata") or {}
                slug = meta.get("slug") or p.get("marketSlug") or p.get("slug") or ""
                if slug != entry.market_slug:
                    continue
                cash = p.get("cashValue") or p.get("currentValue") or p.get("value") or {}
                curr_val = float(cash.get("value") if isinstance(cash, dict) else cash or 0)
                net_pos = abs(float(p.get("netPosition") or p.get("netPositionDecimal") or 0))
                if curr_val > 0 and net_pos > 0:
                    return curr_val / net_pos
                cps = p.get("costPerShare") or {}
                cps_val = float(cps.get("value") if isinstance(cps, dict) else cps or 0)
                if 0 < cps_val <= 1:
                    return cps_val
                raw_pnl = p.get("percentPnl") or p.get("unrealizedPnlPercent") or p.get("pnlPercent")
                if raw_pnl is not None and entry.price > 0:
                    try:
                        pnl = float(raw_pnl)
                        if abs(pnl) > 1:
                            pnl /= 100
                        return entry.price * (1 + pnl)
                    except (TypeError, ValueError):
                        pass
                logger.info("Cannot resolve current price for %s — raw position: %s",
                            entry.market_slug, p)
        except Exception as exc:
            logger.warning("Portfolio price lookup failed for %s: %s", entry.market_slug, exc)
        return None

    async def check_positions(self) -> None:
        await self.sync_from_exchange()
        if not self._entries:
            logger.debug("No tracked positions — skipping TP/SL check")
            return
        logger.info("Checking %d tracked position(s) for TP/SL…", len(self._entries))
        for token_id, entry in list(self._entries.items()):
            current_price = await self._get_price_for_entry(token_id, entry)
            if current_price is None:
                entry.price_misses += 1
                logger.warning(
                    "No price for %s (token=%s…) — miss %d/%d",
                    entry.market_slug, token_id[:12], entry.price_misses, _PRICE_MISS_LIMIT,
                )
                if entry.price_misses >= _PRICE_MISS_LIMIT:
                    logger.error(
                        "Force-closing %s — price unresolvable for %d consecutive cycles",
                        entry.market_slug, _PRICE_MISS_LIMIT,
                    )
                    await self._close(token_id, entry, entry.price, "SL-Force (price unavailable)")
                else:
                    self._save()
                continue
            entry.price_misses = 0
            pnl = (current_price - entry.price) / entry.price if entry.price else 0
            logger.info(
                "Position: %s %s  entry=%.3f  now=%.3f  pnl=%+.1f%%  (TP=%.0f%% SL=%.0f%%)",
                entry.market_slug, entry.side,
                entry.price, current_price, pnl * 100,
                entry.tp * 100, entry.sl * 100,
            )
            try:
                if pnl >= entry.tp:
                    await self._close(token_id, entry, current_price, f"TP +{pnl:.1%}")
                elif pnl <= -entry.sl:
                    await self._close(token_id, entry, current_price, f"SL {pnl:.1%}")
            except Exception as exc:
                logger.error("Unexpected error closing %s: %s", entry.market_slug, exc)

    async def sell_all(self) -> int:
        count = 0
        for token_id, entry in list(self._entries.items()):
            current_price = await self._get_price_for_entry(token_id, entry) or entry.price
            await self._close(token_id, entry, current_price, "Manual /sellall")
            count += 1

        try:
            live_list = list(await self._client.get_open_positions() or [])
        except Exception:
            live_list = []

        tracked_slugs = {e.market_slug for e in self._entries.values()}
        for p in live_list:
            if not isinstance(p, dict):
                continue
            meta = p.get("marketMetadata") or {}
            slug = meta.get("slug") or p.get("marketSlug") or p.get("slug") or ""
            if not slug or slug in tracked_slugs:
                continue
            intent = str(p.get("intent") or p.get("side") or p.get("positionType") or "").upper()
            if "SHORT" in intent or "NO" in intent:
                side = "NO"
            elif "LONG" in intent or "YES" in intent:
                side = "YES"
            else:
                net = float(p.get("netPosition") or p.get("netPositionDecimal") or 0)
                side = "NO" if net < 0 else "YES"
            cash = p.get("cashValue") or p.get("value") or p.get("size") or {}
            size_usd = float(cash.get("value") if isinstance(cash, dict) else cash or 0)
            avg_px = p.get("avgPx") or p.get("avgPrice") or p.get("price") or {}
            price = float(avg_px.get("value") if isinstance(avg_px, dict) else avg_px or 0.50) or 0.50
            if size_usd <= 0:
                continue
            try:
                await self._client.close_position(slug, side, price, size_usd)
                pnl_pct = float(p.get("percentPnl") or p.get("pnl") or 0)
                msg = (
                    f"🏳️ *Position Closed* — Manual /sellall\n"
                    f"Market: `{slug}`\n"
                    f"Side: {side} · Exit: {price:.3f}\n"
                    f"P&L: {'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}%"
                )
                await self._app.bot.send_message(
                    chat_id=self._chat_id, text=msg, parse_mode="Markdown",
                )
                count += 1
            except Exception as exc:
                logger.error("sell_all live close failed for %s: %s", slug, exc)

        return count

    async def sell_half(self) -> int:
        if not self._entries:
            return 0
        count = 0
        for token_id, entry in list(self._entries.items()):
            current_price = await self._get_price_for_entry(token_id, entry) or entry.price
            half_usd = round(entry.amount_usd / 2, 2)
            try:
                await self._client.close_position(
                    entry.market_slug, entry.side, current_price, half_usd
                )
                self._entries[token_id] = _Entry(
                    market_slug = entry.market_slug,
                    side        = entry.side,
                    price       = entry.price,
                    tp          = entry.tp,
                    sl          = entry.sl,
                    amount_usd  = half_usd,
                    conviction  = entry.conviction,
                    triggers    = entry.triggers,
                )
                self._save()
                pnl = (current_price - entry.price) / entry.price if entry.price else 0
                pnl_usd = half_usd * abs(pnl)
                msg = (
                    f"✂️ *Half Sold* — `{entry.market_slug}`\n"
                    f"Sold ${half_usd:.2f} of {entry.side} @ {current_price:.3f}\n"
                    f"P&L on half: {'+'if pnl>=0 else ''}{pnl:.1%} (${pnl_usd:.2f})\n"
                    f"Remaining: ${half_usd:.2f} still open"
                )
                await self._app.bot.send_message(
                    chat_id=self._chat_id, text=msg, parse_mode="Markdown",
                )
                count += 1
            except Exception as exc:
                logger.error("sell_half failed for %s: %s", entry.market_slug, exc)
        return count

    async def _close(
        self, token_id: str, entry: _Entry, current_price: float, reason: str
    ) -> None:
        try:
            await self._client.close_position(
                entry.market_slug, entry.side, current_price, entry.amount_usd
            )
        except Exception as exc:
            logger.error(
                "close_position API call failed for %s: %s — removing from tracker anyway",
                entry.market_slug, exc,
            )
        self._entries.pop(token_id, None)
        self._save()

        pnl = (current_price - entry.price) / entry.price if entry.price else 0
        pnl_usd = entry.amount_usd * pnl
        is_tp = "TP" in reason
        is_manual = "Manual" in reason
        emoji = "✅" if is_tp else ("🏳️" if is_manual else "🔴")

        reserved_now = 0.0
        if is_tp and pnl_usd > 0:
            reserved_now = round(pnl_usd * PROFIT_RESERVE_PCT, 4)
            self._reserve_usd += reserved_now
            self._save_reserve()
            self._day_reserved += reserved_now

        self._day_pnl += pnl_usd
        if is_tp:
            self._day_tp += 1
        elif not is_manual:
            self._day_sl += 1

        if is_tp:
            outcome = "TP"
        elif is_manual:
            outcome = "MANUAL"
        elif "Force" in reason:
            outcome = "FORCE"
        else:
            outcome = "SL"

        self._log_trade(entry, current_price, pnl, pnl_usd, outcome)

        pnl_abs = abs(pnl_usd)
        reserve_line = f"\n💰 Reserved: ${reserved_now:.2f} (total ${self._reserve_usd:.2f})" if reserved_now > 0 else ""
        msg = (
            f"{emoji} *Position Closed* — {reason}\n"
            f"Market: `{entry.market_slug}`\n"
            f"Side: {entry.side} · Entry: {entry.price:.3f} → Exit: {current_price:.3f}\n"
            f"P&L: {'+'if pnl>=0 else ''}{pnl:.1%}  (${'+' if pnl>=0 else '-'}{pnl_abs:.2f})"
            f"{reserve_line}"
        )
        try:
            await self._app.bot.send_message(
                chat_id=self._chat_id, text=msg, parse_mode="Markdown",
            )
        except Exception as exc:
            logger.error("Failed to send close notification: %s", exc)
        logger.info("Closed position %s: %s (reserved $%.4f)", entry.market_slug, reason, reserved_now)
