import asyncio
import logging
from typing import Callable, Awaitable

from polymarket.client import PolymarketClient, Trade, TraderProfile

logger = logging.getLogger(__name__)

AlertCallback = Callable[[str], Awaitable[None]]


def _score_trade(trade: Trade, profile: TraderProfile, copy_min_win_rate: float, copy_min_volume: float) -> tuple[int, str]:
    """Return (score 0-100, recommendation label)."""
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


def _format_trade_alert(
    trade: Trade,
    label: str,
    profile: TraderProfile,
    score: int,
    recommendation: str,
) -> str:
    price_pct = f"{trade.price * 100:.1f}¢"
    side_emoji = "📈 BUY" if trade.side == "BUY" else "📉 SELL"
    win_pct = f"{profile.win_rate * 100:.1f}%"
    vol = f"${profile.total_volume:,.0f}"

    lines = [
        f"🔔 *New Trade Detected*",
        f"",
        f"👤 *Wallet:* `{label}` (`{trade.wallet[:8]}…`)",
        f"📊 *Market:* {trade.market_name}",
        f"🎯 *Outcome:* {trade.outcome}",
        f"",
        f"{side_emoji}  |  Price: {price_pct}  |  Size: {trade.size:.1f} shares",
        f"💵 *Value:* ${trade.usd_value:,.2f}",
        f"",
        f"📋 *Trader Stats*",
        f"  Win Rate: {win_pct}  |  Vol: {vol}  |  Trades: {profile.total_trades}",
        f"",
        f"⚡ *Score:* {score}/100",
        f"{recommendation}",
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

    async def add_wallet(self, address: str, label: str = "") -> None:
        addr = address.lower()
        self._wallets.append(addr)
        self._labels[addr] = label or addr[:8]
        self._seen_ids[addr] = set()

        trades = await self._client.get_recent_trades(addr, limit=50)
        for t in trades:
            self._seen_ids[addr].add(t.id)

        self._profiles[addr] = await self._client.build_trader_profile(addr)
        logger.info("Watching %s (%s) — seeded %d existing trades", label or addr[:8], addr[:8], len(trades))

    async def _refresh_profile(self, addr: str) -> None:
        try:
            self._profiles[addr] = await self._client.build_trader_profile(addr)
        except Exception as exc:
            logger.debug("Profile refresh failed for %s: %s", addr[:8], exc)

    async def _check_wallet(self, addr: str) -> None:
        try:
            trades = await self._client.get_recent_trades(addr, limit=50)
            new_trades = [t for t in trades if t.id not in self._seen_ids[addr]]
            for trade in new_trades:
                self._seen_ids[addr].add(trade.id)
                if not await self._client.is_market_active(trade.market_id):
                    logger.info("Market '%s' is resolved — skipping alert", trade.market_name)
                    continue
                profile = self._profiles.get(addr, TraderProfile(wallet=addr))
                score, rec = _score_trade(trade, profile, self._copy_min_win_rate, self._copy_min_volume)
                if score < 75:
                    logger.info("Trade scored %d/100 — below threshold, skipping alert", score)
                    continue
                label = self._labels.get(addr, addr[:8])
                msg = _format_trade_alert(trade, label, profile, score, rec)
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
