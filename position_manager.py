from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polymarket_client import PolymarketClient
    from telegram.ext import Application

logger = logging.getLogger(__name__)

_PERSIST_FILE = os.path.join(os.path.dirname(__file__), "positions.json")


@dataclass
class _Entry:
    market_slug: str
    side:        str
    price:       float
    tp:          float
    sl:          float
    amount_usd:  float = 0.50


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
        self._load()

    # ── Persistence ───────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            with open(_PERSIST_FILE) as f:
                data = json.load(f)
            for token_id, d in data.items():
                self._entries[token_id] = _Entry(**d)
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

    # ── Public API ────────────────────────────────────────────────────

    def record_entry(
        self,
        token_id:    str,
        market_slug: str,
        side:        str,
        entry_price: float,
        tp_pct:      float | None = None,
        sl_pct:      float | None = None,
        amount_usd:  float = 0.50,
    ) -> None:
        self._entries[token_id] = _Entry(
            market_slug = market_slug,
            side        = side,
            price       = entry_price,
            tp          = tp_pct if tp_pct is not None else self._default_tp,
            sl          = sl_pct if sl_pct is not None else self._default_sl,
            amount_usd  = amount_usd,
        )
        self._save()
        logger.info(
            "Recorded entry: slug=%s side=%s price=%.3f TP=%.0f%% SL=%.0f%%",
            market_slug, side, entry_price,
            (tp_pct or self._default_tp) * 100,
            (sl_pct or self._default_sl) * 100,
        )

    async def check_positions(self) -> None:
        if not self._entries:
            logger.debug("No tracked positions — skipping TP/SL check")
            return
        logger.info("Checking %d tracked position(s) for TP/SL…", len(self._entries))
        for token_id, entry in list(self._entries.items()):
            current_price = await self._client.get_current_price(token_id)
            if current_price is None:
                logger.warning(
                    "No price for %s (token=%s…) — skipping this cycle",
                    entry.market_slug, token_id[:12],
                )
                continue
            pnl = (current_price - entry.price) / entry.price if entry.price else 0
            logger.info(
                "Position: %s %s  entry=%.3f  now=%.3f  pnl=%+.1f%%  (TP=%.0f%% SL=%.0f%%)",
                entry.market_slug, entry.side,
                entry.price, current_price, pnl * 100,
                entry.tp * 100, entry.sl * 100,
            )
            if pnl >= entry.tp:
                await self._close(token_id, entry, current_price, f"TP +{pnl:.1%}")
            elif pnl <= -entry.sl:
                await self._close(token_id, entry, current_price, f"SL {pnl:.1%}")

    async def sell_all(self) -> int:
        """Close every tracked position immediately. Returns number closed."""
        count = 0

        # Close positions tracked in _entries (survive crash-restarts)
        for token_id, entry in list(self._entries.items()):
            current_price = await self._client.get_current_price(token_id) or entry.price
            await self._close(token_id, entry, current_price, "Manual /sellall")
            count += 1

        # Also close any live portfolio positions not in _entries (e.g. after redeploy)
        try:
            live_list = list(await self._client.get_open_positions() or [])
        except Exception:
            live_list = []

        tracked_slugs = {e.market_slug for e in self._entries.values()}
        for p in live_list:
            if not isinstance(p, dict):
                continue
            slug = p.get("marketSlug") or p.get("slug") or ""
            if not slug or slug in tracked_slugs:
                continue
            intent = str(p.get("intent") or p.get("side") or p.get("positionType") or "").upper()
            side = "NO" if ("SHORT" in intent or "NO" in intent) else "YES"
            price = float(p.get("currentPrice") or p.get("price") or p.get("avgPrice") or 0.50)
            size_usd = float(p.get("cashValue") or p.get("value") or p.get("size") or 0)
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
        """Sell half of each tracked position. Returns number of half-sells executed."""
        if not self._entries:
            return 0
        count = 0
        for token_id, entry in list(self._entries.items()):
            current_price = await self._client.get_current_price(token_id) or entry.price
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
        await self._client.close_position(
            entry.market_slug, entry.side, current_price, entry.amount_usd
        )
        self._entries.pop(token_id, None)
        self._save()
        pnl = (current_price - entry.price) / entry.price if entry.price else 0
        pnl_usd = entry.amount_usd * abs(pnl)
        is_tp = "TP" in reason
        is_manual = "Manual" in reason
        emoji = "✅" if is_tp else ("🏳️" if is_manual else "🔴")
        msg = (
            f"{emoji} *Position Closed* — {reason}\n"
            f"Market: `{entry.market_slug}`\n"
            f"Side: {entry.side} · Entry: {entry.price:.3f} → Exit: {current_price:.3f}\n"
            f"P&L: {'+'if pnl>=0 else ''}{pnl:.1%}  (${'+' if pnl>=0 else '-'}{pnl_usd:.2f})"
        )
        try:
            await self._app.bot.send_message(
                chat_id=self._chat_id, text=msg, parse_mode="Markdown",
            )
        except Exception as exc:
            logger.error("Failed to send close notification: %s", exc)
        logger.info("Closed position %s: %s", entry.market_slug, reason)
