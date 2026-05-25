import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
POLY_API = "https://polymarket.com"


@dataclass
class MarketTrend:
    current_price: float       # 0–1
    price_7d_ago: float        # 0–1
    price_1d_ago: float        # 0–1
    change_7d_pct: float       # % change over 7 days
    change_1d_pct: float       # % change over 24 hours
    direction: str             # "bullish" | "bearish" | "sideways"
    success_probability: float # adjusted probability 0–100


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

    async def get_market_trend(self, market_id: str, outcome: str, current_price: float) -> MarketTrend:
        """Fetch price history and compute trend + success probability for a market outcome."""
        now = int(time.time())
        start_7d = now - 7 * 86400
        start_1d = now - 86400

        # Resolve token ID for this outcome from the market
        token_id = await self._resolve_token_id(market_id, outcome)

        history_7d = await self._fetch_price_history(market_id, token_id, start_7d, now, fidelity=480)
        history_1d = await self._fetch_price_history(market_id, token_id, start_1d, now, fidelity=60)

        price_7d_ago = history_7d[0] if history_7d else current_price
        price_1d_ago = history_1d[0] if history_1d else current_price

        change_7d = (current_price - price_7d_ago) / price_7d_ago * 100 if price_7d_ago > 0 else 0.0
        change_1d = (current_price - price_1d_ago) / price_1d_ago * 100 if price_1d_ago > 0 else 0.0

        if change_7d > 3:
            direction = "bullish"
        elif change_7d < -3:
            direction = "bearish"
        else:
            direction = "sideways"

        # Momentum bonus/penalty: trend in the direction of current price
        momentum = min(max(change_7d * 0.3 + change_1d * 0.5, -15), 15)
        success_prob = min(max(current_price * 100 + momentum, 1), 99)

        return MarketTrend(
            current_price=current_price,
            price_7d_ago=price_7d_ago,
            price_1d_ago=price_1d_ago,
            change_7d_pct=change_7d,
            change_1d_pct=change_1d,
            direction=direction,
            success_probability=success_prob,
        )

    async def _resolve_token_id(self, market_id: str, outcome: str) -> str | None:
        data = await self._get(f"{GAMMA_API}/markets", params={"id": market_id})
        if not data:
            data = await self._get(f"{GAMMA_API}/markets/{market_id}")
        market = None
        if isinstance(data, list) and data:
            market = data[0]
        elif isinstance(data, dict) and "id" in data:
            market = data
        if not market:
            return None
        tokens = market.get("clobTokenIds") or market.get("tokens") or []
        if isinstance(tokens, str):
            import json
            try:
                tokens = json.loads(tokens)
            except Exception:
                return None
        outcome_lower = outcome.lower()
        for i, tok in enumerate(tokens):
            label = ""
            if isinstance(tok, dict):
                label = tok.get("outcome", tok.get("label", "")).lower()
                tid = tok.get("token_id") or tok.get("id") or ""
            else:
                tid = str(tok)
                label = "yes" if i == 0 else "no"
            if outcome_lower in label or label in outcome_lower:
                return tid
        return tokens[0] if tokens else None

    async def _fetch_price_history(self, market_id: str, token_id: str | None, start_ts: int, end_ts: int, fidelity: int = 60) -> list[float]:
        params: dict = {"startTs": start_ts, "endTs": end_ts, "fidelity": fidelity}
        if token_id:
            params["tokenId"] = token_id
        else:
            params["market"] = market_id
        data = await self._get(f"{CLOB_API}/prices-history", params=params)
        if not data:
            return []
        history = data.get("history") or data if isinstance(data, list) else []
        prices = []
        for point in history:
            if isinstance(point, dict):
                p = point.get("p") or point.get("price") or point.get("value")
            else:
                p = point
            try:
                prices.append(float(p))
            except (TypeError, ValueError):
                pass
        return prices

    async def get_market_end_date(self, market_id: str) -> str:
        """Return the market resolution/end date as a formatted string. Always returns a value."""
        if not market_id:
            return "N/A"

        # Try Gamma API with id param, then direct endpoint, then conditionId
        market = None
        for attempt in [
            lambda: self._get(f"{GAMMA_API}/markets", params={"id": market_id}),
            lambda: self._get(f"{GAMMA_API}/markets/{market_id}"),
            lambda: self._get(f"{GAMMA_API}/markets", params={"conditionId": market_id}),
            lambda: self._get(f"{GAMMA_API}/events", params={"id": market_id}),
        ]:
            data = await attempt()
            if isinstance(data, list) and data:
                market = data[0]
                break
            elif isinstance(data, dict) and ("id" in data or "endDate" in data or "resolutionTime" in data):
                market = data
                break

        if not market:
            return "N/A"

        # Try every known date field in priority order
        raw = (
            market.get("endDate")
            or market.get("endDateIso")
            or market.get("resolutionTime")
            or market.get("closeTime")
            or market.get("end_date")
            or market.get("expirationDate")
            or market.get("expiration")
            or ""
        )

        if not raw:
            return "N/A"

        try:
            from datetime import datetime, timezone
            if isinstance(raw, (int, float)):
                dt = datetime.fromtimestamp(float(raw), tz=timezone.utc)
            else:
                raw_str = str(raw).replace("Z", "+00:00")
                dt = datetime.fromisoformat(raw_str)
            return dt.strftime("%b %d, %Y")
        except Exception:
            # Return whatever raw string we got, trimmed
            return str(raw)[:10] or "N/A"

    async def is_market_us_accessible(self, market_id: str) -> bool:
        """Returns True if the market is not restricted for US users."""
        if not market_id:
            return True
        cache_key = f"us:{market_id}"
        if cache_key in self._market_status_cache:
            return self._market_status_cache[cache_key]

        data = await self._get(f"{GAMMA_API}/markets", params={"id": market_id})
        if not data:
            data = await self._get(f"{GAMMA_API}/markets/{market_id}")
        market = None
        if isinstance(data, list) and data:
            market = data[0]
        elif isinstance(data, dict) and "id" in data:
            market = data

        if market is None:
            self._market_status_cache[cache_key] = True
            return True

        # Explicit US restriction flag
        if market.get("restricted") or market.get("umaResolutionStatus") == "restricted":
            self._market_status_cache[cache_key] = False
            return False

        # Block markets by restricted category or tags
        _BLOCKED_KEYWORDS = {
            "us politics", "us elections", "us election", "us political",
            "united states politics", "american politics",
            "us congress", "us senate", "us house", "us president",
            "trump", "harris", "biden", "2024 election", "2026 election",
        }
        category = (market.get("category") or "").lower()
        tags = [t.lower() if isinstance(t, str) else (t.get("label") or t.get("name") or "").lower()
                for t in (market.get("tags") or [])]
        title = (market.get("question") or market.get("title") or "").lower()

        combined = f"{category} {' '.join(tags)} {title}"
        for kw in _BLOCKED_KEYWORDS:
            if kw in combined:
                logger.info("Market blocked for US: '%s' (matched '%s')", market.get("question", market_id), kw)
                self._market_status_cache[cache_key] = False
                return False

        self._market_status_cache[cache_key] = True
        return True

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
