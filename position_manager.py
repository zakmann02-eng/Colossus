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
            return
        positions = await self._client.get_open_positions()
        live = {
            (p.get("asset") or p.get("tokenId") or p.get("marketSlug") or ""): p
            for p in positions
        }
        for token_id, entry in list(self._entries.items()):
            pos = live.get(token_id) or live.get(entry.market_slug)
            if not pos:
                self._entries.pop(token_id, None)
                self._save()
                continue
            current_price = float(
                pos.get("currentPrice") or pos.get("price") or entry.price
            )
            pnl = (current_price - entry.price) / entry.price
            if pnl >= entry.tp:
                await self._close(token_id, entry, current_price, f"TP +{pnl:.1%}")
            elif pnl <= -entry.sl:
                await self._close(token_id, entry, current_price, f"SL {pnl:.1%}")

    async def _close(
        self, token_id: str, entry: _Entry, current_price: float, reason: str
    ) -> None:
        size_usd = entry.amount_usd
        resp = await self._client.close_position(
            entry.market_slug, entry.side, current_price, size_usd
        )
        self._entries.pop(token_id, None)
        self._save()
        msg = (
            f"🔔 *Position Closed* ({reason})\n"
            f"Market: `{entry.market_slug}`\n"
            f"Entry: {entry.price:.3f} → Current: {current_price:.3f}\n"
            f"Result: {'✅' if 'TP' in reason else '🔴'} {reason}"
        )
        try:
            await self._app.bot.send_message(
                chat_id=self._chat_id, text=msg, parse_mode="Markdown",
            )
        except Exception as exc:
            logger.error("Failed to send close notification: %s", exc)
        logger.info("Closed position %s: %s", entry.market_slug, reason)
