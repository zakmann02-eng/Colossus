"""
Polymarket API client — market data only.
No private key required. Uses public APIs for market scanning and pricing.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

# ── Hard-block keywords ──────────────────────────────────────────────────────

_BLOCKED = {
    "politics", "election", "president", "congress", "senate",
    "trump", "harris", "biden", "democrat", "republican",
    "crypto", "bitcoin", "ethereum", "solana", "btc", "eth",
    "war", "conflict", "invasion", "missile", "nato",
    "fed rate", "interest rate", "inflation", "gdp",
    "oscar", "grammy", "emmy", "celebrity", "reality tv",
}

_ALLOWED_SPORTS = {
    "nfl", "ncaa football", "college football",
    "nba", "ncaa basketball", "college basketball",
    "premier league", "champions league", "world cup", "mls",
    "la liga", "bundesliga", "serie a", "ligue 1", "soccer", "football",
    "mlb", "baseball",
    "ufc", "mma", "boxing",
}


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
    # Market scanning                                                   #
    # ---------------------------------------------------------------- #

    async def get_sports_markets(self, limit: int = 200) -> list[dict]:
        data = await self._get(
            f"{GAMMA_API}/markets",
            params={"active": "true", "closed": "false", "limit": limit,
                    "order": "volume24hr", "ascending": "false"},
        )
        markets = data if isinstance(data, list) else (data or {}).get("data", []) if data else []
        return [m for m in markets if self._is_allowed(m)]

    def _is_allowed(self, market: dict) -> bool:
        text = " ".join([
            (market.get("question") or "").lower(),
            (market.get("title") or "").lower(),
            (market.get("category") or "").lower(),
            " ".join(
                t.lower() if isinstance(t, str)
                else (t.get("label") or t.get("name") or "").lower()
                for t in (market.get("tags") or [])
            ),
        ])
        for kw in _BLOCKED:
            if kw in text:
                return False
        for kw in _ALLOWED_SPORTS:
            if kw in text:
                return True
        return False

    # ---------------------------------------------------------------- #
    # Price history (T2 — 15-min price movement)                       #
    # ---------------------------------------------------------------- #

    async def get_price_15min_ago(self, market: dict, token_id: str) -> float | None:
        now = int(time.time())
        start = now - 20 * 60
        params = {"tokenId": token_id, "startTs": start, "endTs": now, "fidelity": 1}
        data = await self._get(f"{CLOB_API}/prices-history", params=params)
        if not data:
            params = {"market": market.get("id", ""), "startTs": start, "endTs": now, "fidelity": 1}
            data = await self._get(f"{CLOB_API}/prices-history", params=params)
        history = (data or {}).get("history") or (data if isinstance(data, list) else [])
        if not history:
            return None
        try:
            return float(history[0].get("p") or history[0].get("price") or 0)
        except Exception:
            return None

    # ---------------------------------------------------------------- #
    # Current price                                                     #
    # ---------------------------------------------------------------- #

    async def get_current_price(self, token_id: str) -> float | None:
        data = await self._get(f"{CLOB_API}/last-trade-price", params={"token_id": token_id})
        if data:
            try:
                return float(data.get("price") or 0) or None
            except Exception:
                pass
        book = await self._get(f"{CLOB_API}/book", params={"token_id": token_id})
        if book:
            try:
                bids = book.get("bids") or []
                asks = book.get("asks") or []
                best_bid = float(bids[0]["price"]) if bids else 0
                best_ask = float(asks[0]["price"]) if asks else 0
                if best_bid and best_ask:
                    return (best_bid + best_ask) / 2
            except Exception:
                pass
        return None

    # ---------------------------------------------------------------- #
    # Token ID / market helpers                                         #
    # ---------------------------------------------------------------- #

    def resolve_token_id(self, market: dict, side: str) -> str | None:
        tokens = market.get("clobTokenIds") or market.get("tokens") or []
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except Exception:
                return None
        if not tokens:
            return None
        idx = 0 if side == "YES" else 1
        tok = tokens[idx] if idx < len(tokens) else tokens[0]
        if isinstance(tok, dict):
            return tok.get("token_id") or tok.get("id")
        return str(tok)

    def get_event_date(self, market: dict) -> str:
        raw = (
            market.get("startDate") or market.get("start_date")
            or market.get("endDate") or market.get("endDateIso")
            or market.get("resolutionTime") or ""
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

    def seconds_to_resolution(self, market: dict) -> float | None:
        raw = market.get("resolutionTime") or market.get("endDate") or market.get("closeTime")
        if not raw:
            return None
        try:
            if isinstance(raw, (int, float)):
                end_ts = float(raw)
            else:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                end_ts = dt.timestamp()
            return end_ts - time.time()
        except Exception:
            return None

    def market_url(self, market: dict) -> str:
        slug = market.get("slug") or market.get("marketSlug") or ""
        if slug:
            return f"https://polymarket.com/event/{slug}"
        mid = market.get("id") or ""
        return f"https://polymarket.com/event/{mid}" if mid else "https://polymarket.com"
