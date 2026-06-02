"""
TP / SL monitor — checks open positions every 5 minutes.
TP and SL thresholds are per-trade based on conviction level.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polymarket_client import PolymarketClient
    from telegram.ext import Application

logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    price: float
    tp:    float   # take profit %
    sl:    float   # stop loss %


class PositionManager:
    def __init__(
        self,
        client: "PolymarketClient",
        app: "Application",
        chat_id: int,
        default_tp: float = 0.10,
        default_sl: float = 0.10,
    ) -> None:
        self._client     = client
        self._app        = app
        self._chat_id    = chat_id
        self._default_tp = default_tp
        self._default_sl = default_sl
        self._entries: dict[str, _Entry] = {}

    def record_entry(
        self,
        token_id: str,
        entry_price: float,
        tp_pct: float | None = None,
        sl_pct: float | None = None,
    ) -> None:
        self._entries[token_id] = _Entry(
            price = entry_price,
            tp    = tp_pct if tp_pct is not None else self._default_tp,
            sl    = sl_pct if sl_pct is not None else self._default_sl,
        )
        logger.debug(
            "Entry %s @ %.4f | TP=%.0f%% SL=%.0f%%",
            token_id[:12], entry_price, self._entries[token_id].tp * 100,
            self._entries[token_id].sl * 100,
        )

    async def check_positions(self) -> None:
        positions = await self._client.get_open_positions()
        if not positions:
            return

        for pos in positions:
            token_id = (
                pos.get("asset")
                or pos.get("tokenId")
                or pos.get("token_id")
                or ""
            )
            size = float(pos.get("size") or pos.get("amount") or 0)
            if not token_id or size <= 0:
                continue

            current = await self._client.get_current_price(token_id)
            if current is None:
                continue

            entry_obj = self._entries.get(token_id)
            if entry_obj is None:
                avg_raw = pos.get("avgPrice") or pos.get("average_price")
                if avg_raw:
                    entry_obj = _Entry(
                        price = float(avg_raw),
                        tp    = self._default_tp,
                        sl    = self._default_sl,
                    )
                    self._entries[token_id] = entry_obj
                else:
                    continue

            pnl_pct = (current - entry_obj.price) / entry_obj.price

            if pnl_pct >= entry_obj.tp:
                await self._close_and_alert(
                    token_id, size, entry_obj.price, current, "TAKE PROFIT", pnl_pct, entry_obj
                )
            elif pnl_pct <= -entry_obj.sl:
                await self._close_and_alert(
                    token_id, size, entry_obj.price, current, "STOP LOSS", pnl_pct, entry_obj
                )

    async def _close_and_alert(
        self,
        token_id: str,
        size: float,
        entry: float,
        current: float,
        reason: str,
        pnl_pct: float,
        entry_obj: _Entry,
    ) -> None:
        resp = await self._client.close_position(token_id, size)
        self._entries.pop(token_id, None)

        emoji  = "✅" if pnl_pct >= 0 else "🛑"
        status = "filled" if resp else "failed"
        msg = (
            f"{emoji} *{reason}*\n"
            f"Token: `{token_id[:12]}…`\n"
            f"Entry: {entry:.3f} → Exit: {current:.3f}\n"
            f"P&L: {pnl_pct:+.1%}\n"
            f"Target was: TP {entry_obj.tp:.0%} / SL {entry_obj.sl:.0%}\n"
            f"Close order: {status}"
        )
        try:
            await self._app.bot.send_message(
                chat_id    = self._chat_id,
                text       = msg,
                parse_mode = "Markdown",
            )
        except Exception as exc:
            logger.error("Telegram send failed: %s", exc)

        logger.info(
            "%s token=%s pnl=%.2f%% close=%s",
            reason, token_id[:12], pnl_pct * 100, status,
        )
