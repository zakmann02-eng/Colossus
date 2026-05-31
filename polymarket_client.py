"""Polymarket API client."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

_SPORTS_KEYWORDS = {
    "nba", "nfl", "mlb", "nhl", "mls", "ufc", "nascar",
    "basketball", "football", "baseball", "hockey", "soccer",
    "tennis", "golf", "boxing", "mma", "cricket", "rugby",
    "champions league", "premier league", "la liga", "bundesliga",
    "world cup", "super bowl", "playoffs", "championship",
    "match", "game", "series", "tournament",
}

_BLOCKED_KEYWORDS = {
    "us politics", "us elections", "us election", "us political",
    "united states politics", "american politics",
    "us congress", "us senate", "us house", "us president",
    "trump", "harris", "biden",
}


@dataclass
class MarketTrend:
    direction: str          # bullish | bearish | sideways
    change_7d_pct: float
    change_1d_pct: float
    success_probability: float


class PolymarketClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._s = session
        self._market_cache: dict[str, dict] = {}

    # ---------------------------------------------------------------- #
    # Core HTTP                                                         #
    # ---------------------------------------------------------------- #

    async def _get(self, url: str, params: dict | None = None) -> Any:
        try:
            async with self._s.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                r.raise_for_status()
                return await r.json()
        except Exception as exc:
            logger.debug("GET %s failed: %s", url, exc)
            return None

    # ---------------------------------------------------------------- #
    # Wallet data                                                       #
    # ---------------------------------------------------------------- #

    async def get_wallet_trades(self, wallet: str, limit: int = 20) -> list[dict]:
        data = await self._get(
            f"{DATA_API}/activity",
            params={"user": wallet, "limit": limit, "offset": 0},
        )
        if not data:
            return []
        return data if isinstance(data, list) else data.get("data", [])

    async def get_wallet_positions(self, wallet: str) -> list[dict]:
        data = await self._get(
            f"{DATA_API}/positions",
            params={"user": wallet, "sizeThreshold": "0.01"},
        )
        if not data:
            return []
        return data if isinstance(data, list) else data.get("data", [])

    # ---------------------------------------------------------------- #
    # Market resolution                                                 #
    # ---------------------------------------------------------------- #

    async def get_market_by_token(self, token_id: str) -> dict | None:
        if token_id in self._market_cache:
            return self._market_cache[token_id]
        data = await self._get(f"{CLOB_API}/markets/{token_id}")
        if not data and token_id:
            data = await self._get(f"{GAMMA_API}/markets", params={"clobTokenIds": token_id})
        market = self._extract_market(data)
        if market:
            self._market_cache[token_id] = market
        return market

    async def get_market(self, condition_id: str) -> dict | None:
        if condition_id in self._market_cache:
            return self._market_cache[condition_id]
        data = await self._get(f"{GAMMA_API}/markets", params={"id": condition_id})
        if not data:
            data = await self._get(f"{GAMMA_API}/markets/{condition_id}")
        if not data:
            data = await self._get(f"{GAMMA_API}/markets", params={"conditionId": condition_id})
        market = self._extract_market(data)
        if market:
            self._market_cache[condition_id] = market
        return market

    def _extract_market(self, data: Any) -> dict | None:
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict) and ("id" in data or "question" in data):
            return data
        return None

    # ---------------------------------------------------------------- #
    # Market helpers (sync — called after market is resolved)          #
    # ---------------------------------------------------------------- #

    def is_market_active(self, market: dict) -> bool:
        return not (market.get("closed") or market.get("resolved"))

    def is_market_us_accessible(self, market: dict) -> bool:
        combined = " ".join([
            (market.get("category") or "").lower(),
            (market.get("question") or market.get("title") or "").lower(),
            " ".join(
                t.lower() if isinstance(t, str)
                else (t.get("label") or t.get("name") or "").lower()
                for t in (market.get("tags") or [])
            ),
        ])
        # Sports always pass
        for kw in _SPORTS_KEYWORDS:
            if kw in combined:
                return True
        # Hard block on political restricted markets
        if market.get("restricted"):
            return False
        for kw in _BLOCKED_KEYWORDS:
            if kw in combined:
                return False
        return True

    def get_event_date(self, market: dict) -> str:
        """Return the game/event date. Prioritises startDate (actual game time)."""
        raw = (
            market.get("startDate")
            or market.get("start_date")
            or market.get("gameDate")
            or market.get("endDate")
            or market.get("endDateIso")
            or market.get("resolutionTime")
            or market.get("closeTime")
            or ""
        )
        if not raw:
            return "N/A"
        try:
            if isinstance(raw, (int, float)):
                dt = datetime.fromtimestamp(float(raw), tz=timezone.utc)
            else:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return dt.strftime("%b %d, %Y")
        except Exception:
            return str(raw)[:10] or "N/A"

    # ---------------------------------------------------------------- #
    # Market trend                                                      #
    # ---------------------------------------------------------------- #

    async def get_market_trend(self, market: dict, trade: dict) -> MarketTrend | None:
        try:
            current_price = self._extract_price(trade)
            if current_price <= 0:
                return None

            market_id = market.get("id") or market.get("conditionId") or ""
            token_id = await self._resolve_token_id(market, trade)
            now = int(time.time())

            h7d = await self._price_history(market_id, token_id, now - 7 * 86400, now, 480)
            h1d = await self._price_history(market_id, token_id, now - 86400, now, 60)

            p7d = h7d[0] if h7d else current_price
            p1d = h1d[0] if h1d else current_price

            c7d = (current_price - p7d) / p7d * 100 if p7d > 0 else 0.0
            c1d = (current_price - p1d) / p1d * 100 if p1d > 0 else 0.0

            direction = "bullish" if c7d > 3 else ("bearish" if c7d < -3 else "sideways")
            momentum = min(max(c7d * 0.3 + c1d * 0.5, -15), 15)
            prob = min(max(current_price * 100 + momentum, 1), 99)

            return MarketTrend(
                direction=direction,
                change_7d_pct=c7d,
                change_1d_pct=c1d,
                success_probability=prob,
            )
        except Exception as exc:
            logger.debug("Trend fetch failed: %s", exc)
            return None

    async def _resolve_token_id(self, market: dict, trade: dict) -> str | None:
        # Try to get from trade first
        tid = trade.get("asset_id") or trade.get("assetId") or trade.get("tokenId") or ""
        if tid:
            return tid
        # Fall back to first token in market
        tokens = market.get("clobTokenIds") or market.get("tokens") or []
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except Exception:
                return None
        if tokens:
            t = tokens[0]
            return t.get("token_id") or t.get("id") or str(t) if isinstance(t, dict) else str(t)
        return None

    async def _price_history(
        self, market_id: str, token_id: str | None, start: int, end: int, fidelity: int
    ) -> list[float]:
        params: dict = {"startTs": start, "endTs": end, "fidelity": fidelity}
        if token_id:
            params["tokenId"] = token_id
        else:
            params["market"] = market_id
        data = await self._get(f"{CLOB_API}/prices-history", params=params)
        if not data:
            return []
        history = data.get("history") or (data if isinstance(data, list) else [])
        prices = []
        for pt in history:
            p = pt.get("p") or pt.get("price") or pt.get("value") if isinstance(pt, dict) else pt
            try:
                prices.append(float(p))
            except (TypeError, ValueError):
                pass
        return prices

    def _extract_price(self, trade: dict) -> float:
        for key in ("price", "outcomePrice", "avgPrice"):
            v = trade.get(key)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return 0.0
