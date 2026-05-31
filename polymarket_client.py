"""
Polymarket API client — market data + trade execution via API keys.
Uses Key ID + Secret from polymarket.us/developer (no private key needed).
"""

from __future__ import annotations

import asyncio
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
POLYGON_CHAIN_ID = 137

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
    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
    ) -> None:
        self._s = session
        self._api_key        = api_key.strip()
        self._api_secret     = api_secret.strip()
        self._api_passphrase = api_passphrase.strip()
        self._clob_client    = self._init_clob_client()
        self._market_cache: dict[str, dict] = {}

    def _init_clob_client(self):
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
            creds = ApiCreds(
                api_key        = self._api_key,
                api_secret     = self._api_secret,
                api_passphrase = self._api_passphrase,
            )
            client = ClobClient(
                host     = CLOB_API,
                chain_id = POLYGON_CHAIN_ID,
                creds    = creds,
            )
            logger.info("CLOB client initialised with API key %s…", self._api_key[:8])
            return client
        except Exception as exc:
            logger.error("Failed to init CLOB client: %s", exc)
            return None

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
    # Trade execution                                                   #
    # ---------------------------------------------------------------- #

    async def place_market_order(
        self, token_id: str, side: str, amount_usd: float
    ) -> dict | None:
        if not self._clob_client:
            logger.error("CLOB client not initialised — cannot place order")
            return None
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, self._sync_market_order, token_id, side, amount_usd
            )
            return result
        except Exception as exc:
            logger.error("Order placement failed: %s", exc)
            return None

    def _sync_market_order(self, token_id: str, side: str, amount_usd: float) -> dict | None:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        try:
            from py_clob_client.clob_types import BUY, SELL
            s = BUY if side == "BUY" else SELL
        except ImportError:
            s = 0 if side == "BUY" else 1  # newer versions use int constants
        args   = MarketOrderArgs(token_id=token_id, amount=amount_usd, side=s)
        signed = self._clob_client.create_market_order(args)
        resp   = self._clob_client.post_order(signed, OrderType.FOK)
        logger.info("Order response: %s", resp)
        return resp

    async def close_position(self, token_id: str, size: float) -> dict | None:
        if not self._clob_client:
            return None
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None, self._sync_close_position, token_id, size
            )
        except Exception as exc:
            logger.error("Close position failed: %s", exc)
            return None

    def _sync_close_position(self, token_id: str, size: float) -> dict | None:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        try:
            from py_clob_client.clob_types import SELL
        except ImportError:
            SELL = 1
        args   = MarketOrderArgs(token_id=token_id, amount=size, side=SELL)
        signed = self._clob_client.create_market_order(args)
        return self._clob_client.post_order(signed, OrderType.FOK)

    # ---------------------------------------------------------------- #
    # Open positions                                                    #
    # ---------------------------------------------------------------- #

    async def get_open_positions(self) -> list[dict]:
        if not self._clob_client:
            return []
        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(None, self._clob_client.get_positions)
            if not data:
                return []
            return data if isinstance(data, list) else data.get("data", [])
        except Exception as exc:
            logger.debug("get_open_positions failed: %s", exc)
            return []

    # ---------------------------------------------------------------- #
    # Helpers                                                           #
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
