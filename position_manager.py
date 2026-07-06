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
_RESERVE_FILE   = Path("reserve.json")
_TRADE_LOG      = Path("trade_log.csv")
_CSV_HEADERS    = [
    "timestamp", "market_slug", "side", "entry_price", "exit_price",
    "pnl_pct", "reason", "conviction", "triggers",
]
_RESERVE_PCT = 0.10  # 10% of each winning trade's profit goes to reserve


@dataclass
class _Entry:
    market_slug:  str
    side:         str
    price:        float   # token price (YES token for YES; NO token for NO)
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
        self._reserve_usd: float = 0.0
        self._init_trade_log()
        self._load()
        self._load_reserve()

    # ── Persistence ────────────────────────────────────────────────────────────

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

    def _load_reserve(self) -> None:
        try:
            if _RESERVE_FILE.exists():
                self._reserve_usd = float(json.loads(_RESERVE_FILE.read_text()).get("reserve_usd", 0.0))
                logger.info("Reserve loaded: $%.2f", self._reserve_usd)
        except Exception as exc:
            logger.warning("Could not load reserve.json: %s", exc)

    def _save_reserve(self) -> None:
        try:
            _RESERVE_FILE.write_text(json.dumps({"reserve_usd": round(self._reserve_usd, 4)}, indent=2))
        except Exception as exc:
            logger.warning("Could not save reserve.json: %s", exc)

    def get_reserve(self) -> float:
        return self._reserve_usd

    def _init_trade_log(self) -> None:
        if not _TRADE_LOG.exists():
            try:
                with _TRADE_LOG.open("w", newline="") as f:
                    csv.writer(f).writerow(_CSV_HEADERS)
            except Exception as exc:
                logger.warning("Could not init trade_log.csv: %s", exc)

    def _log_trade(
        self,
        entry: _Entry,
        exit_price: float,
        pnl_pct: float,
        reason: str,
    ) -> None:
        from datetime import datetime, timezone
        row = [
            datetime.now(timezone.utc).isoformat(),
            entry.market_slug,
            entry.side,
            f"{entry.price:.4f}",
            f"{exit_price:.4f}",
            f"{pnl_pct:.2%}",
            reason,
            entry.conviction,
            entry.triggers,
        ]
        try:
            with _TRADE_LOG.open("a", newline="") as f:
                csv.writer(f).writerow(row)
        except Exception as exc:
            logger.warning("Could not write trade_log.csv: %s", exc)

    # ── Entry recording ────────────────────────────────────────────────────────

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
        # entry_price from the analyzer is the YES-token market price.
        # Convert to the actual token price so CLOB comparisons are consistent:
        #   YES position → store YES price (identity)
        #   NO position  → store NO price (= 1 - YES price)
        token_price = entry_price if side == "YES" else (1.0 - entry_price)
        self._entries[token_id] = _Entry(
            market_slug = market_slug,
            side        = side,
            price       = token_price,
            tp          = tp_pct if tp_pct is not None else self._default_tp,
            sl          = sl_pct if sl_pct is not None else self._default_sl,
            amount_usd  = amount_usd,
            conviction  = conviction,
            triggers    = ", ".join(triggers) if triggers else "",
        )
        self._save()
        logger.info(
            "Recorded entry: slug=%s side=%s YES_price=%.3f token_price=%.3f "
            "TP=%.0f%% SL=%.0f%% conviction=%s",
            market_slug, side, entry_price, token_price,
            (tp_pct or self._default_tp) * 100,
            (sl_pct or self._default_sl) * 100,
            conviction,
        )

    def has_position(self, market_slug: str) -> bool:
        return any(e.market_slug == market_slug for e in self._entries.values())

    # ── Token ID resolution ────────────────────────────────────────────────────

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

    # ── Exchange sync (runs every check cycle) ─────────────────────────────────

    async def sync_from_exchange(self) -> None:
        try:
            positions = await self._client.get_open_positions()
        except Exception as exc:
            logger.warning("sync_from_exchange: could not fetch positions: %s", exc)
            return

        tracked_slugs = {e.market_slug for e in self._entries.values()}

        for pos in positions:
            slug = (
                pos.get("marketSlug")
                or pos.get("slug")
                or pos.get("market_slug")
                or ""
            )
            if not slug or slug in tracked_slugs or slug in self._closed_this_session:
                continue

            token_id = (
                pos.get("asset")
                or pos.get("tokenId")
                or pos.get("token_id")
                or pos.get("clobTokenId")
                or slug
            )

            net = float(pos.get("netPosition") or pos.get("size") or 0)
            side = "YES" if net > 0 else "NO"

            cost = pos.get("cost") or {}
            cost_val = float(
                cost.get("value") if isinstance(cost, dict) else cost or 0
            )
            # avgPrice from exchange API is typically the YES-side probability price.
            yes_price = float(
                pos.get("avgPrice") or pos.get("price") or
                (cost_val / abs(net) if abs(net) > 0 else 0.50)
            )
            if yes_price <= 0:
                yes_price = 0.50

            # Convert to token price for consistent P&L tracking
            token_price = yes_price if side == "YES" else (1.0 - yes_price)

            self._entries[token_id] = _Entry(
                market_slug = slug,
                side        = side,
                price       = token_price,
                tp          = self._default_tp,
                sl          = self._default_sl,
                amount_usd  = cost_val or abs(net) * yes_price,
                conviction  = "SYNCED",
                triggers    = "exchange_sync",
            )
            tracked_slugs.add(slug)
            logger.info(
                "sync_from_exchange: registered %s side=%s yes_price=%.3f token_price=%.3f",
                slug, side, yes_price, token_price,
            )

        self._save()

    # ── TP/SL monitoring ───────────────────────────────────────────────────────

    async def check_positions(self) -> None:
        await self.sync_from_exchange()

        if not self._entries:
            logger.debug("No tracked positions — skipping TP/SL check")
            return

        logger.info("TP/SL check: %d tracked position(s)", len(self._entries))

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

            # CLOB last-trade price for this specific token (YES or NO)
            current_price = await self._client.get_current_price(token_id)

            if current_price is None:
                # Fallback to portfolio YES price; convert to token space.
                pos_data = live_by_slug.get(entry.market_slug, {})
                yes_price = float(pos_data.get("currentPrice") or pos_data.get("price") or 0)
                if yes_price > 0 and abs(yes_price - 0.500) > 0.02:
                    current_price = (1.0 - yes_price) if entry.side == "NO" else yes_price
                    logger.info(
                        "Portfolio fallback %s: side=%s yes=%.3f → token_price=%.3f entry=%.3f",
                        entry.market_slug, entry.side, yes_price, current_price, entry.price,
                    )
                else:
                    logger.info("No reliable price for %s (token=%s…) — skipping cycle",
                                entry.market_slug, str(token_id)[:16])
                    continue

            # 0.500 is the CLOB sentinel for thin books with no real trades.
            if abs(current_price - 0.500) < 0.001:
                pos_data = live_by_slug.get(entry.market_slug, {})
                yes_price = float(pos_data.get("currentPrice") or pos_data.get("price") or 0)
                if yes_price > 0 and abs(yes_price - 0.500) > 0.02:
                    current_price = (1.0 - yes_price) if entry.side == "NO" else yes_price
                    logger.info(
                        "0.500 sentinel fallback %s: side=%s yes=%.3f → token_price=%.3f entry=%.3f",
                        entry.market_slug, entry.side, yes_price, current_price, entry.price,
                    )
                else:
                    logger.info("Price is 0.500 sentinel for %s — skipping TP/SL", entry.market_slug)
                    continue

            # P&L in token-price space — entry.price and current_price are both token prices.
            pnl = (current_price - entry.price) / entry.price
            logger.info(
                "TP/SL check %s: side=%s token_price=%.3f entry=%.3f pnl=%+.1f%% TP=%.0f%% SL=%.0f%%",
                entry.market_slug, entry.side, current_price, entry.price,
                pnl * 100, entry.tp * 100, entry.sl * 100,
            )

            if pnl >= entry.tp:
                await self._close(token_id, entry, current_price, f"TP +{pnl:.1%}")
            elif pnl <= -entry.sl:
                await self._close(token_id, entry, current_price, f"SL {pnl:.1%}")

    async def _close(
        self, token_id: str, entry: _Entry, current_price: float, reason: str
    ) -> None:
        # current_price is the token price (YES for YES positions, NO for NO positions).
        pnl = (current_price - entry.price) / entry.price
        resp = await self._client.close_position(
            entry.market_slug, entry.side, current_price, entry.amount_usd
        )
        logger.info("Close response for %s: %s", entry.market_slug, resp)

        self._entries.pop(token_id, None)
        self._closed_this_session.add(entry.market_slug)
        executions = (resp or {}).get("executions") or [] if isinstance(resp, dict) else []
        if not executions:
            logger.warning(
                "Close order for %s had no executions — order resting on exchange",
                entry.market_slug,
            )

        self._save()
        self._log_trade(entry, current_price, pnl, reason)

        if pnl > 0:
            profit_usd = entry.amount_usd * pnl
            contribution = round(profit_usd * _RESERVE_PCT, 4)
            self._reserve_usd += contribution
            self._save_reserve()
            logger.info("Reserve +$%.4f (10%% of $%.4f profit) → total $%.2f",
                        contribution, profit_usd, self._reserve_usd)

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

    # ── Partial close (sell half on strong TP) ─────────────────────────────────

    async def sell_half(self, token_id: str, entry: _Entry, current_price: float) -> None:
        half_usd = entry.amount_usd / 2
        resp = await self._client.close_position(
            entry.market_slug, entry.side, current_price, half_usd
        )
        logger.info("Sell-half response for %s: %s", entry.market_slug, resp)

        self._entries[token_id] = _Entry(
            market_slug = entry.market_slug,
            side        = entry.side,
            price       = entry.price,
            tp          = entry.tp,
            sl          = entry.sl,
            amount_usd  = half_usd,
            price_misses= entry.price_misses,
            conviction  = entry.conviction,
            triggers    = entry.triggers,
        )
        self._save()

    # ── Daily P&L report ───────────────────────────────────────────────────────

    async def get_report(self) -> str:
        positions = await self._client.get_open_positions()
        lines = [f"📊 Daily Report — {len(self._entries)} tracked position(s)\n"]

        live_by_slug = {
            (p.get("marketSlug") or p.get("slug") or ""): p
            for p in positions
        }

        for token_id, entry in self._entries.items():
            pos = live_by_slug.get(entry.market_slug)
            if pos:
                # Portfolio returns YES price; convert to token space for P&L.
                yes_price = float(pos.get("currentPrice") or pos.get("price") or 0)
                if yes_price > 0:
                    current_token = yes_price if entry.side == "YES" else (1.0 - yes_price)
                else:
                    current_token = entry.price
            else:
                current_token = entry.price
            pnl = (current_token - entry.price) / entry.price
            emoji = "🟢" if pnl >= 0 else "🔴"
            lines.append(
                f"{emoji} {entry.market_slug[:24]}  {entry.side} "
                f"@ {entry.price:.3f} → {current_token:.3f} ({pnl:+.1%}) "
                f"[{entry.conviction}]"
            )

        return "\n".join(lines)
