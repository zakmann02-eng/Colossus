"""
TP / SL monitor — checks open positions every 30 minutes and closes at ±10%.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polymarket_client import PolymarketClient
    from telegram.ext import Application

logger = logging.getLogger(__name__)

TAKE_PROFIT_DEFAULT = 0.10   # +10 %
STOP_LOSS_DEFAULT   = 0.10   # −10 %


class PositionManager:
    def __init__(
        self,
        client: "PolymarketClient",
        app: "Application",  # telegram Application, for sending messages
        chat_id: int,
        tp_pct: float = TAKE_PROFIT_DEFAULT,
        sl_pct: float = STOP_LOSS_DEFAULT,
    ) -> None:
        self._client   = client
        self._app      = app
        self._chat_id  = chat_id
        self._tp       = tp_pct
        self._sl       = sl_pct

        # token_id → entry_price  (populated when we open a trade)
        self._entries: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # Called by bot.py when a trade is opened                             #
    # ------------------------------------------------------------------ #

    def record_entry(self, token_id: str, entry_price: float) -> None:
        self._entries[token_id] = entry_price
        logger.debug("Recorded entry %s @ %.4f", token_id[:12], entry_price)

    # ------------------------------------------------------------------ #
    # Scheduled job — runs every 30 min                                   #
    # ------------------------------------------------------------------ #

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

            entry = self._entries.get(token_id)
            if entry is None:
                # Position not tracked in this session — use avg_price if available
                avg_raw = pos.get("avgPrice") or pos.get("average_price")
                if avg_raw:
                    entry = float(avg_raw)
                    self._entries[token_id] = entry
                else:
                    continue

            pnl_pct = (current - entry) / entry

            if pnl_pct >= self._tp:
                await self._close_and_alert(token_id, size, entry, current, "TAKE PROFIT", pnl_pct)
            elif pnl_pct <= -self._sl:
                await self._close_and_alert(token_id, size, entry, current, "STOP LOSS", pnl_pct)

    # ------------------------------------------------------------------ #
    # Internal                                                            #
    # ------------------------------------------------------------------ #

    async def _close_and_alert(
        self,
        token_id: str,
        size: float,
        entry: float,
        current: float,
        reason: str,
        pnl_pct: float,
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

        logger.info("%s token=%s pnl=%.2f%% close=%s", reason, token_id[:12], pnl_pct * 100, status)
