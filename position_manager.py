from __future__ import annotations

import csv
import html
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polymarket_client import PolymarketClient
    from telegram.ext import Application

logger = logging.getLogger(__name__)

_POSITIONS_FILE = Path("positions.json")
_TRADE_LOG      = Path("trade_log.csv")
_CSV_HEADERS    = [
    "timestamp", "market_slug", "side", "entry_price", "exit_price",
    "pnl_pct", "reason", "conviction", "triggers",
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
        self._client              = client
        self._app                 = app
        self._chat_id             = chat_id
        self._default_tp          = default_tp
        self._default_sl          = default_sl
        self._entries: dict[str, _Entry] = {}
        self._closed_this_session: set[str] = set()
        self._init_trade_log()
        self._load()

    def _load(self) -> None:
        if not _POSITIONS_FILE.exists():
            return
        try:
            raw = json.loads(_POSITIONS_FILE.read_text())
            for token_id, d in raw.items():
                self._entries[token_id] = _Entry(**d)
            logger.info("Loaded %d positions from %s", len(self._entries), _POSITIONS_FILE)
        except Exception as exc:
            logger.warning("Could not load positions.json: %s", exc)

    def _save(self) -> None:
        try:
            data = {tid: asdict(e) for tid, e in self._entries.items()}
            _POSITIONS_FILE.write_text(json.dumps(data, indent=2))
        except Exception as exc:
            logger.warning("Could not save positions.json: %s", exc)

    def _init_trade_log(self) -> None:
        if not _TRADE_LOG.exists():
            try:
                with _TRADE_LOG.open("w", newline="") as f:
                    csv.writer(f).writerow(_CSV_HEADERS)
            except Exception as exc:
                logger.warning("Could not init trade_log.csv: %s", exc)

    def _log_trade(self, entry: _Entry, exit_price: float, pnl_pct: float, reason: str) -> None:
        from datetime import datetime, timezone
        row = [
            datetime.now(timezone.utc).isoformat(),
            entry.market_slug, entry.side,
            f"{entry.price:.4f}", f"{exit_price:.4f}",
            f"{pnl_pct:.2%}", reason, entry.conviction, entry.triggers,
        ]
        try:
            with _TRADE_LOG.open("a", newline="") as f:
                csv.writer(f).writerow(row)
        except Exception as exc:
            logger.warning("Could not write trade_log.csv: %s", exc)

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
        triggers:    list | None = None,
    ) -> None:
        self._entries[token_id] = _Entry(
            market_slug = market_slug,
            side        = side,
            price       = entry_price,
            tp          = tp_pct if tp_pct is not None else self._default_tp,
            sl          = sl_pct if sl_pct is not None else self._default_sl,
            amount_usd  = amount_usd,
            conviction  = conviction,
            triggers    = ", ".join(triggers) if triggers else "",
        )
        self._save()
        logger.info(
            "Recorded entry: slug=%s side=%s price=%.3f TP=%.0f%% SL=%.0f%% conviction=%s",
            market_slug, side, entry_price,
            (tp_pct or self._default_tp) * 100,
            (sl_pct or self._default_sl) * 100,
            conviction,
        )

    def update_token_ids_from_markets(self, markets: list, client) -> None:
        slug_to_token: dict[str, str] = {}
        for market in markets:
            slug = client.get_market_slug(market)
            if not slug:
                continue
            token_id = client.resolve_token_id(market, "YES")
            if token_id and len(str(token_id)) >= 32:
                slug_to_token[slug] = token_id

        updated = 0
        for old_token_id in list(self._entries.keys()):
            entry = self._entries[old_token_id]
            real_token_id = slug_to_token.get(entry.market_slug)
            if real_token_id and real_token_id != old_token_id:
                self._entries[real_token_id] = entry
                del self._entries[old_token_id]
                updated += 1
                logger.info("token_id updated for %s: %s…", entry.market_slug, real_token_id[:16])

        if updated:
            self._save()
            logger.info("update_token_ids_from_markets: updated %d position(s)", updated)

    async def sync_from_exchange(self) -> None:
        try:
            positions = await self._client.get_open_positions()
        except Exception as exc:
            logger.warning("sync_from_exchange: could not fetch positions: %s", exc)
            return

        tracked_slugs = {e.market_slug for e in self._entries.values()}

        for pos in positions:
            slug = (
                pos.get("marketSlug") or pos.get("slug")
                or pos.get("market_slug") or ""
            )
            if not slug or slug in tracked_slugs or slug in self._closed_this_session:
                continue

            token_id = (
                pos.get("asset") or pos.get("tokenId")
                or pos.get("token_id") or pos.get("clobTokenId") or slug
            )

            net = float(pos.get("netPosition") or pos.get("size") or 0)
            side = "YES" if net > 0 else "NO"

            cost = pos.get("cost") or {}
            cost_val = float(cost.get("value") if isinstance(cost, dict) else cost or 0)
            entry_price = float(
                pos.get("avgPrice") or pos.get("price") or
                (cost_val / abs(net) if abs(net) > 0 else 0.50)
            )
            if entry_price <= 0:
                entry_price = 0.50

            self._entries[token_id] = _Entry(
                market_slug = slug,
                side        = side,
                price       = entry_price,
                tp          = self._default_tp,
                sl          = self._default_sl,
                amount_usd  = cost_val or abs(net) * entry_price,
                conviction  = "SYNCED",
                triggers    = "exchange_sync",
            )
            tracked_slugs.add(slug)
            logger.info("sync_from_exchange: registered %s side=%s price=%.3f", slug, side, entry_price)

        self._save()

    async def check_positions(self) -> None:
        await self.sync_from_exchange()

        if not self._entries:
            logger.debug("No tracked positions — skipping TP/SL check")
            return

        positions = await self._client.get_open_positions()
        exchange_has_data = len(positions) > 0
        live_by_slug = {
            (p.get("marketSlug") or p.get("slug") or ""): p
            for p in positions
        }

        for token_id, entry in list(self._entries.items()):
            if exchange_has_data and entry.market_slug not in live_by_slug:
                logger.info("Position %s confirmed closed on exchange — removing", entry.market_slug)
                self._entries.pop(token_id, None)
                self._save()
                continue

            current_price = await self._client.get_current_price(token_id)
            if current_price is None:
                logger.debug("No CLOB price for %s — skipping cycle", entry.market_slug)
                continue

            pnl = (current_price - entry.price) / entry.price
            logger.debug("TP/SL check %s: price=%.3f entry=%.3f pnl=%+.1f%%",
                         entry.market_slug, current_price, entry.price, pnl * 100)

            if pnl >= entry.tp:
                await self._close(token_id, entry, current_price, f"TP +{pnl:.1%}")
            elif pnl <= -entry.sl:
                await self._close(token_id, entry, current_price, f"SL {pnl:.1%}")

    async def _close(self, token_id: str, entry: _Entry, current_price: float, reason: str) -> None:
        pnl = (current_price - entry.price) / entry.price
        resp = await self._client.close_position(
            entry.market_slug, entry.side, current_price, entry.amount_usd
        )
        logger.info("Close response for %s: %s", entry.market_slug, resp)

        self._entries.pop(token_id, None)

        # Always block re-sync after a close attempt — unfilled close orders
        # still exist on the exchange and the position should not be re-entered.
        self._closed_this_session.add(entry.market_slug)
        executions = (resp or {}).get("executions") or [] if isinstance(resp, dict) else []
        if not executions:
            logger.warning(
                "Close order for %s had no executions — order resting on exchange",
                entry.market_slug,
            )

        self._save()
        self._log_trade(entry, current_price, pnl, reason)

        slug_safe       = html.escape(entry.market_slug)
        conviction_safe = html.escape(str(entry.conviction))
        triggers_safe   = html.escape(str(entry.triggers))
        msg = (
            f"🔔 <b>Position Closed</b> ({reason})\n"
            f"Market: <code>{slug_safe}</code>\n"
            f"Entry: {entry.price:.3f} → Current: {current_price:.3f}\n"
            f"Conviction: {conviction_safe} | {triggers_safe}\n"
            f"Result: {'✅' if 'TP' in reason else '🔴'} {reason}"
        )
        try:
            await self._app.bot.send_message(
                chat_id=self._chat_id, text=msg, parse_mode="HTML",
            )
        except Exception as exc:
            logger.error("Failed to send close notification: %s", exc)
        logger.info("Closed position %s: %s", entry.market_slug, reason)

    async def sell_half(self, token_id: str, entry: _Entry, current_price: float) -> None:
        half_usd = entry.amount_usd / 2
        resp = await self._client.close_position(
            entry.market_slug, entry.side, current_price, half_usd
        )
        logger.info("Sell-half response for %s: %s", entry.market_slug, resp)
        self._entries[token_id] = _Entry(
            market_slug  = entry.market_slug,
            side         = entry.side,
            price        = entry.price,
            tp           = entry.tp,
            sl           = entry.sl,
            amount_usd   = half_usd,
            price_misses = entry.price_misses,
            conviction   = entry.conviction,
            triggers     = entry.triggers,
        )
        self._save()

    async def get_report(self) -> str:
        positions = await self._client.get_open_positions()
        lines = [f"📊 *Daily Report* — {len(self._entries)} tracked position(s)\n"]
        live_by_slug = {
            (p.get("marketSlug") or p.get("slug") or ""): p
            for p in positions
        }
        for token_id, entry in self._entries.items():
            pos = live_by_slug.get(entry.market_slug)
            if pos:
                current_price = float(pos.get("currentPrice") or pos.get("price") or entry.price)
            else:
                current_price = entry.price
            pnl = (current_price - entry.price) / entry.price
            emoji = "🟢" if pnl >= 0 else "🔴"
            lines.append(
                f"{emoji} `{entry.market_slug[:24]}` {entry.side} "
                f"@ {entry.price:.3f} → {current_price:.3f} ({pnl:+.1%}) "
                f"[{entry.conviction}]"
            )
        return "\n".join(lines)
