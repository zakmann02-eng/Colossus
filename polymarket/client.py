import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
POLY_API = "https://polymarket.com"


@dataclass
class Trade:
    id: str
    wallet: str
    market_id: str
    market_name: str
    outcome: str
    side: str          # BUY or SELL
    price: float       # 0–1 (probability cents)
    size: float        # shares
    usd_value: float
    timestamp: int


@dataclass
class Position:
    wallet: str
    market_id: str
    market_name: str
    outcome: str
    size: float
    avg_price: float
    current_price: float
    usd_value: float
    unrealized_pnl_pct: float


@dataclass
class TraderProfile:
    wallet: str
    total_trades: int = 0
    win_rate: float = 0.0
    total_volume: float = 0.0
    total_pnl: float = 0.0


class PolymarketClient:
    def __init__(self, session: aiohttp.ClientSession):
        self._s = session
        self._market_status_cache: dict[str, bool] = {}  # market_id -> is_active

    async def _get(self, url: str, params: dict | None = None) -> Any:
        try:
            async with self._s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                r.raise_for_status()
                return await r.json()
        except Exception as exc:
            logger.debug("GET %s failed: %s", url, exc)
            return None

    async def is_market_active(self, market_id: str) -> bool:
        """Returns True only if the market is still open and unresolved."""
        if not market_id:
            return True
        if market_id in self._market_status_cache:
            return self._market_status_cache[market_id]
        data = await self._get(f"{GAMMA_API}/markets", params={"id": market_id})
        if not data:
            data = await self._get(f"{GAMMA_API}/markets/{market_id}")
        market = None
        if isinstance(data, list) and data:
            market = data[0]
        elif isinstance(data, dict) and "id" in data:
            market = data
        if market is None:
            return True
        closed = market.get("closed", False) or market.get("resolved", False)
        active = not closed
        self._market_status_cache[market_id] = active
        return active

    async def resolve_username(self, username: str) -> str | None:
        data = await self._get(f"{GAMMA_API}/users", params={"username": username})
        if data and isinstance(data, list) and data:
            return data[0].get("proxyWallet", "").lower() or None
        profile = await self._get(f"{POLY_API}/profile/{username}")
        if isinstance(profile, dict):
            return profile.get("proxyWallet", "").lower() or None
        return None

    async def get_recent_trades(self, wallet: str, limit: int = 50) -> list[Trade]:
        data = await self._get(
            f"{DATA_API}/activity",
            params={"user": wallet, "limit": limit, "offset": 0},
        )
        if not data:
            return []
        trades = []
        for item in data if isinstance(data, list) else data.get("data", []):
            t = self._parse_trade(wallet, item)
            if t:
                trades.append(t)
        return trades

    async def get_open_positions(self, wallet: str) -> list[Position]:
        data = await self._get(f"{DATA_API}/positions", params={"user": wallet, "sizeThreshold": "0.01"})
        if not data:
            return []
        positions = []
        for item in data if isinstance(data, list) else data.get("data", []):
            p = self._parse_position(wallet, item)
            if p:
                positions.append(p)
        return positions

    async def build_trader_profile(self, wallet: str) -> TraderProfile:
        trades = await self.get_recent_trades(wallet, limit=200)
        profile = TraderProfile(wallet=wallet, total_trades=len(trades))
        if not trades:
            return profile

        profile.total_volume = sum(t.usd_value for t in trades)
        wins = 0
        for t in trades:
            if t.side == "SELL" and t.price > 0.5:
                wins += 1
        sells = [t for t in trades if t.side == "SELL"]
        profile.win_rate = wins / len(sells) if sells else 0.0
        return profile

    def _parse_trade(self, wallet: str, item: dict) -> Trade | None:
        try:
            tid = item.get("id") or item.get("transactionHash") or ""
            market_id = (
                item.get("market") or item.get("conditionId") or item.get("marketId") or ""
            )
            market_name = (
                item.get("title") or item.get("question") or item.get("marketTitle") or market_id[:20]
            )
            outcome = item.get("outcome") or item.get("outcomeIndex") or "?"
            if isinstance(outcome, int):
                outcome = "YES" if outcome == 0 else "NO"
            side = (item.get("side") or item.get("type") or "BUY").upper()
            price = float(item.get("price") or item.get("outcomePrice") or 0)
            size = float(item.get("size") or item.get("shares") or 0)
            usd = float(item.get("usdcSize") or item.get("amount") or (price * size))
            ts = int(item.get("timestamp") or item.get("createdAt") or 0)
            return Trade(
                id=str(tid),
                wallet=wallet,
                market_id=str(market_id),
                market_name=str(market_name),
                outcome=str(outcome),
                side=side,
                price=price,
                size=size,
                usd_value=usd,
                timestamp=ts,
            )
        except Exception as exc:
            logger.debug("Failed to parse trade: %s — %s", item, exc)
            return None

    def _parse_position(self, wallet: str, item: dict) -> Position | None:
        try:
            market_id = item.get("market") or item.get("conditionId") or ""
            market_name = item.get("title") or item.get("question") or market_id[:20]
            outcome = item.get("outcome") or "?"
            if isinstance(outcome, int):
                outcome = "YES" if outcome == 0 else "NO"
            size = float(item.get("size") or item.get("shares") or 0)
            avg_price = float(item.get("avgPrice") or item.get("averagePrice") or 0)
            cur_price = float(item.get("currentPrice") or item.get("price") or avg_price)
            usd = size * cur_price
            pnl_pct = ((cur_price - avg_price) / avg_price * 100) if avg_price > 0 else 0.0
            return Position(
                wallet=wallet,
                market_id=str(market_id),
                market_name=str(market_name),
                outcome=str(outcome),
                size=size,
                avg_price=avg_price,
                current_price=cur_price,
                usd_value=usd,
                unrealized_pnl_pct=pnl_pct,
            )
        except Exception as exc:
            logger.debug("Failed to parse position: %s — %s", item, exc)
            return None
