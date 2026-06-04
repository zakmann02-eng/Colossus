"""
Polymarket API client — market data + trade execution.
Uses polymarket-us SDK for authenticated trading on Polymarket.US.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

_BLOCKED = {
    "politics", "election", "president", "congress", "senate",
    "trump", "harris", "biden", "democrat", "republican",
    "war", "conflict", "invasion", "missile", "nato",
    "fed rate", "interest rate", "inflation", "gdp",
    "oscar", "grammy", "emmy", "celebrity", "reality tv",
}


class PolymarketClient:
    def __init__(self, session, key_id: str, secret_key: str) -> None:
        self._s          = session
        self._key_id     = key_id.strip()
        self._secret_key = secret_key.strip()
        self._us_client  = self._init_us_client()
        self._market_cache: dict = {}
        logger.info("PolymarketClient ready — key_id %s…", self._key_id[:8])

    def _init_us_client(self):
        try:
            from polymarket_us import PolymarketUS
            client = PolymarketUS(
                key_id=self._key_id,
                secret_key=self._secret_key,
            )
            logger.info("Polymarket.US client initialised — key_id %s…", self._key_id[:8])
            return client
        except Exception as exc:
            logger.error("Failed to init Polymarket.US client: %s", exc)
            return None

    async def _get(self, url, params=None):
        try:
            async with self._s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                r.raise_for_status()
                return await r.json()
        except Exception as exc:
            logger.debug("GET %s failed: %s", url, exc)
            return None

    async def get_sports_markets(self, limit=200):
        data = await self._get(
            f"{GAMMA_API}/markets",
            params={"active": "true", "closed": "false", "limit": limit,
                    "order": "volume24hr", "ascending": "false"},
        )
        markets = data if isinstance(data, list) else (data or {}).get("data", []) if data else []
        return [m for m in markets if self._is_allowed(m)]

    def _is_allowed(self, market):
        if not market.get("active", True) or market.get("closed", False):
            return False
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
        return True

    async def get_price_15min_ago(self, market, token_id):
        now   = int(time.time())
        start = now - 20 * 60
        data  = await self._get(
            f"{CLOB_API}/prices-history",
            params={"tokenId": token_id, "startTs": start, "endTs": now, "fidelity": 1},
        )
        if not data:
            data = await self._get(
                f"{CLOB_API}/prices-history",
                params={"market": market.get("id", ""), "startTs": start, "endTs": now, "fidelity": 1},
            )
        history = (data or {}).get("history") or (data if isinstance(data, list) else [])
        if not history:
            return None
        try:
            return float(history[0].get("p") or history[0].get("price") or 0)
        except Exception:
            return None

    async def get_current_price(self, token_id):
        data = await self._get(f"{CLOB_API}/last-trade-price", params={"token_id": token_id})
        if data:
            try:
                return float(data.get("price") or 0) or None
            except Exception:
                pass
        book = await self._get(f"{CLOB_API}/book", params={"token_id": token_id})
        if book:
            try:
                bids     = book.get("bids") or []
                asks     = book.get("asks") or []
                best_bid = float(bids[0]["price"]) if bids else 0
                best_ask = float(asks[0]["price"]) if asks else 0
                if best_bid and best_ask:
                    return (best_bid + best_ask) / 2
            except Exception:
                pass
        return None

    async def get_balance(self) -> float:
        if not self._us_client:
            return 999.0
        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(None, self._us_client.account.balances)
            logger.info("Balance response: %s", data)
                    if isinstance(data, dict):
                balances = data.get("balances")
        if balances and isinstance(balances, list):
                    b = balances[0]
                    val = b.get("buyingPower") or b.get("currentBalance") or 0
                    return float(val)
                val = data.get("cash") or data.get("balance") or data.get("availableBalance") or 0
                return float(val)
            return 999.0   
        except Exception as exc:
            logger.warning("get_balance failed: %s — skipping balance check", exc)
            return 999.0

    async def get_open_positions(self):
        if not self._us_client:
            return []
        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(None, self._us_client.portfolio.positions)
            if not data:
                return []
            return data if isinstance(data, list) else data.get("positions", [])
        except Exception as exc:
            logger.debug("get_open_positions failed: %s", exc)
            return []

    async def place_market_order(
        self, market_slug: str, side: str, price: float, amount_usd: float
    ) -> dict | None:
        if not self._us_client:
            logger.error("Polymarket.US client not initialised — cannot place order")
            return None
        if not market_slug:
            logger.error("No market slug — cannot place order")
            return None
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None, self._sync_place_order, market_slug, side, price, amount_usd
            )
        except Exception as exc:
            logger.error("Order placement failed: %s", exc)
            return None

    def _sync_place_order(
        self, market_slug: str, side: str, price: float, amount_usd: float
    ) -> dict | None:
        from polymarket_us import AuthenticationError, BadRequestError, NotFoundError
        intent   = "ORDER_INTENT_BUY_LONG" if side == "YES" else "ORDER_INTENT_BUY_SHORT"
        quantity = max(1, round(amount_usd / price))
        try:
            resp = self._us_client.orders.create({
                "marketSlug": market_slug,
                "intent":     intent,
                "type":       "ORDER_TYPE_LIMIT",
                "price":      {"value": str(round(price, 4)), "currency": "USD"},
                "quantity":   quantity,
                "tif":        "TIME_IN_FORCE_GOOD_TILL_CANCEL",
            })
            logger.info("US order placed: %s", resp)
            return resp
        except AuthenticationError as exc:
            logger.error("Auth error: %s", exc)
        except BadRequestError as exc:
            logger.error("Bad request: %s", exc)
        except NotFoundError as exc:
            logger.error("Market not found (%s): %s", market_slug, exc)
        except Exception as exc:
            logger.error("Order error: %s", exc)
        return None

    async def close_position(
        self, market_slug: str, side: str, price: float, size_usd: float
    ) -> dict | None:
        close_side = "NO" if side == "YES" else "YES"
        return await self.place_market_order(market_slug, close_side, price, size_usd)

    def resolve_token_id(self, market, side):
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

    def get_market_slug(self, market: dict) -> str:
        return market.get("slug") or market.get("marketSlug") or ""

    def get_event_date(self, market):
        raw = (market.get("startDate") or market.get("start_date") or
               market.get("endDate") or market.get("endDateIso") or
               market.get("resolutionTime") or "")
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

    def seconds_to_resolution(self, market):
        raw = market.get("resolutionTime") or market.get("endDate") or market.get("closeTime")
        if not raw:
            return None
        try:
            end_ts = (float(raw) if isinstance(raw, (int, float))
                      else datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp())
            return end_ts - time.time()
        except Exception:
            return None

    def market_url(self, market):
        slug = market.get("slug") or market.get("marketSlug") or ""
        if slug:
            return f"https://polymarket.com/event/{slug}"
        mid = market.get("id") or ""
        return f"https://polymarket.com/event/{mid}" if mid else "https://polymarket.com"
