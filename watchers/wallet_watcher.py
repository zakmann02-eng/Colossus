import asyncio
import logging
from typing import Callable, Awaitable

from polymarket.client import PolymarketClient, Trade, Position, TraderProfile

logger = logging.getLogger(__name__)

AlertCallback = Callable[[str], Awaitable[None]]


def _score_trade(trade: Trade, profile: TraderProfile, copy_min_win_rate: float, copy_min_volume: float) -> tuple[int, str]:
    score = 50

    if profile.win_rate >= 0.70:
        score += 25
    elif profile.win_rate >= 0.55:
        score += 10

    if profile.total_volume >= copy_min_volume * 2:
        score += 15
    elif profile.total_volume >= copy_min_volume:
        score += 7

    if trade.usd_value >= 1000:
        score += 10
    elif trade.usd_value >= 500:
        score += 5

    if trade.price <= 0.25 or trade.price >= 0.75:
        score -= 5

    score = max(0, min(100, score))

    if score >= 75 and profile.win_rate >= copy_min_win_rate and profile.total_volume >= copy_min_volume:
        rec = "🟢 STRONG BUY — copy this trade"
    elif score >= 55 and profile.win_rate >= copy_min_win_rate:
        rec = "🟡 CONSIDER — trader has solid stats"
    else:
        rec = "🔴 SKIP — insufficient track record"

    return score, rec


def _position_key(market_id: str, outcome: str) -> str:
    return f"{market_id}:{outcome}".lower()


def _format_entry_alert(trade: Trade, label: str, profile: TraderProfile, score: int, rec: str, is_new: bool) -> str:
    action = "🟢 *NEW ENTRY*" if is_new else "🟢 *ADDING TO POSITION*"
    lines = [
        f"{action}",
        f"",
        f"👤 *Wallet:* `{label}` (`{trade.wallet[:8]}…`)",
        f"📊 *Market:* {trade.market_name}",
        f"🎯 *Outcome:* {trade.outcome}",
        f"",
        f"📈 BUY  |  Price: {trade.price * 100:.1f}¢  |  Size: {trade.size:.1f} shares",
        f"💵 *Value:* ${trade.usd_value:,.2f}",
        f"",
        f"📋 *Trader Stats*",
        f"  Win Rate: {profile.win_rate * 100:.1f}%  |  Vol: ${profile.total_volume:,.0f}  |  Trades: {profile.total_trades}",
        f"",
        f"⚡ *Score:* {score}/100  {rec}",
    ]
    return "\n".join(lines)


def _format_exit_alert(trade: Trade, label: str, profile: TraderProfile, avg_entry: float | None, is_full: bool) -> str:
    action = "🔴 *FULL EXIT*" if is_full else "🔴 *PARTIAL EXIT*"
    lines = [
        f"{action}",
        f"",
        f"👤 *Wallet:* `{label}` (`{trade.wallet[:8]}…`)",
        f"📊 *Market:* {trade.market_name}",
        f"🎯 *Outcome:* {trade.outcome}",
        f"",
        f"📉 SELL  |  Price: {trade.price * 100:.1f}¢  |  Size: {trade.size:.1f} shares",
        f"💵 *Value:* ${trade.usd_value:,.2f}",
    ]

    if avg_entry and avg_entry > 0:
        pnl_pct = (trade.price - avg_entry) / avg_entry * 100
        pnl_usd = (trade.price - avg_entry) * trade.size
        sign = "+" if pnl_pct >= 0 else ""
        lines += [
            f"",
            f"📊 *P&L*",
            f"  Entry: {avg_entry * 100:.1f}¢  →  Exit: {trade.price * 100:.1f}¢",
            f"  {sign}{pnl_pct:.1f}%  |  {sign}${pnl_usd:,.2f}",
        ]

    lines += [
        f"",
        f"📋 *Trader Stats*",
        f"  Win Rate: {profile.win_rate * 100:.1f}%  |  Vol: ${profile.total_volume:,.0f}  |  Trades: {profile.total_trades}",
    ]
    return "\n".join(lines)


class WalletWatcher:
    def __init__(
        self,
        client: PolymarketClient,
        poll_interval: int = 30,
        copy_min_win_rate: float = 0.55,
        copy_min_volume: float = 10_000.0,
        alert_callback: AlertCallback | None = None,
    ):
        self._client = client
        self._poll_interval = poll_interval
        self._copy_min_win_rate = copy_min_win_rate
        self._copy_min_volume = copy_min_volume
        self._alert_cb = alert_callback

        self._wallets: list[str] = []
        self._labels: dict[str, str] = {}
        self._seen_ids: dict[str, set[str]] = {}
        self._profiles: dict[str, TraderProfile] = {}
        # wallet -> {position_key -> Position}
        self._positions: dict[str, dict[str, Position]] = {}

    async def add_wallet(self, address: str, label: str = "") -> None:
        addr = address.lower()
        self._wallets.append(addr)
        self._labels[addr] = label or addr[:8]
        self._seen_ids[addr] = set()

        trades = await self._client.get_recent_trades(addr, limit=50)
        for t in trades:
            self._seen_ids[addr].add(t.id)

        self._profiles[addr] = await self._client.build_trader_profile(addr)
        self._positions[addr] = await self._fetch_positions(addr)
        logger.info(
            "Watching %s (%s) — seeded %d trades, %d open positions",
            label or addr[:8], addr[:8], len(trades), len(self._positions[addr]),
        )

    async def _fetch_positions(self, addr: str) -> dict[str, Position]:
        positions = await self._client.get_open_positions(addr)
        return {_position_key(p.market_id, p.outcome): p for p in positions}

    async def _refresh_profile(self, addr: str) -> None:
        try:
            self._profiles[addr] = await self._client.build_trader_profile(addr)
        except Exception as exc:
            logger.debug("Profile refresh failed for %s: %s", addr[:8], exc)

    async def _check_wallet(self, addr: str) -> None:
        try:
            trades = await self._client.get_recent_trades(addr, limit=50)
            new_trades = [t for t in trades if t.id not in self._seen_ids[addr]]
            if not new_trades:
                return

            # Snapshot positions before and after processing new trades
            before = self._positions.get(addr, {})
            after = await self._fetch_positions(addr)
            self._positions[addr] = after

            for trade in new_trades:
                self._seen_ids[addr].add(trade.id)

                if not await self._client.is_market_active(trade.market_id):
                    logger.info("Market '%s' is resolved — skipping", trade.market_name)
                    continue

                profile = self._profiles.get(addr, TraderProfile(wallet=addr))
                label = self._labels.get(addr, addr[:8])
                pkey = _position_key(trade.market_id, trade.outcome)

                if trade.side == "BUY":
                    score, rec = _score_trade(trade, profile, self._copy_min_win_rate, self._copy_min_volume)
                    if score < 75:
                        logger.info("Entry scored %d/100 — below threshold, skipping", score)
                        continue
                    is_new = pkey not in before
                    msg = _format_entry_alert(trade, label, profile, score, rec, is_new)

                else:  # SELL / EXIT
                    prev = before.get(pkey)
                    avg_entry = prev.avg_price if prev else None
                    is_full = pkey not in after
                    msg = _format_exit_alert(trade, label, profile, avg_entry, is_full)

                if self._alert_cb:
                    await self._alert_cb(msg)
                else:
                    logger.info("ALERT:\n%s", msg)

        except Exception as exc:
            logger.warning("Error checking wallet %s: %s", addr[:8], exc)

    async def post_positions_summary(self, alert_cb: AlertCallback | None = None) -> None:
        cb = alert_cb or self._alert_cb
        for addr in self._wallets:
            try:
                positions = await self._client.get_open_positions(addr)
                label = self._labels.get(addr, addr[:8])
                if not positions:
                    msg = f"📂 *{label}* — no open positions"
                else:
                    lines = [f"📂 *Open Positions — {label}* (`{addr[:8]}…`)\n"]
                    for p in positions:
                        pnl_sign = "+" if p.unrealized_pnl_pct >= 0 else ""
                        lines.append(
                            f"• {p.market_name}\n"
                            f"  {p.outcome} | {p.size:.1f} shares @ {p.avg_price*100:.1f}¢ → {p.current_price*100:.1f}¢\n"
                            f"  Value: ${p.usd_value:,.2f}  PnL: {pnl_sign}{p.unrealized_pnl_pct:.1f}%"
                        )
                    msg = "\n".join(lines)
                if cb:
                    await cb(msg)
            except Exception as exc:
                logger.warning("Positions fetch failed for %s: %s", addr[:8], exc)

    async def run_forever(self) -> None:
        refresh_counter = 0
        while True:
            for addr in self._wallets:
                await self._check_wallet(addr)

            refresh_counter += 1
            if refresh_counter % 10 == 0:
                for addr in self._wallets:
                    await self._refresh_profile(addr)

            await asyncio.sleep(self._poll_interval)
