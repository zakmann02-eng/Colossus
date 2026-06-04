"""
Polymarket API client — market data + trade execution.
Uses API Key + Secret from polymarket.us/developer for authenticated trading.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
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

_BLOCKED = {
    "politics", "election", "president", "congress", "senate",
    "trump", "harris", "biden", "democrat", "republican",
    "war", "conflict", "invasion", "missile", "nato",
    "fed rate", "interest rate", "inflation", "gdp",
    "oscar", "grammy", "emmy", "celebrity", "reality tv",
}


def _l1_headers(api_key, secret, passphrase, method, path, body=""):
    timestamp = str(int(time.time()))
    message   = timestamp + method.upper() + path + body
    signature = base64.b64encode(
        hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "POLY-API-KEY":    api_key,
        "POLY-SIGNATURE":  signature,
        "POLY-TIMESTAMP":  timestamp,
        "POLY-PASSPHRASE": passphrase,
        "Content-Type":    "application/json",
    }


class PolymarketClient:
    def __init__(self, session, api_key, api_secret, api_passphrase):
        self._s              = session
        self._api_key        = api_key.strip()
        self._api_secret     = api_secret.strip()
        self._api_passphrase = api_passphrase.strip()
        self._clob_client    = self._init_clob_client()
        self._market_cache   = {}
        logger.info("PolymarketClient ready — API key %s…", self._api_key[:8])

    def _init_clob_client(self):
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
            creds = ApiCreds(api_key=self._api_key, api_secret=self._api_secret, api_passphrase=self._api_passphrase)
            client = ClobClient(host=CLOB_API, chain_id=POLYGON_CHAIN_ID, creds=creds)
            logger.info("CLOB client initialised with API key %s…", self._api_key[:8])
            return client
        except Exception as exc:
            logger.error("Failed to init CLOB client: %s", exc)
            return None

    async def _get(self, url, params=None):
        try:
            async with self._s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                r.raise_for_status()
                return await r.json()
        except Exception as exc:
            logger.debug("GET %s failed: %s", url, exc)
            return None

    async def _post(self, path, body):
        body_str = json.dumps(body)
        headers  = _l1_headers(self._api_key, self._api_secret, self._api_passphrase, "POST", path, body_str)
        try:
            async with self._s.post(f"{CLOB_API}{path}", data=body_str, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                resp = await r.json()
                if r.status not in (200, 201):
                    logger.error("POST %s → %s: %s", path, r.status, resp)
                return resp
        except Exception as exc:
            logger.error("POST %s failed: %s", path, exc)
            return None

    async def get_sports_markets(self, limit=200):
        data = await self._get(f"{GAMMA_API}/markets", params={"active": "true", "closed": "false", "limit": limit, "order": "volume24hr", "ascending": "false"})
        markets = data if isinstance(data, list) else (data or {}).get("data", []) if data else []
        return [m for m in markets if self._is_allowed(m)]

    def _is_allowed(self, market):
        if not market.get("active", True) or market.get("closed", False):
            return False
        text = " ".join([
            (market.get("question") or "").lower(),
            (market.get("title") or "").lower(),
            (market.get("category") or "").lower(),
            " ".join(t.lower() if isinstance(t, str) else (t.get("label") or t.get("name") or "").lower() for t in (market.get("tags") or [])),
        ])
        for kw in _BLOCKED:
            if kw in text:
                return False
        return True

    async def get_price_15min_ago(self, market, token_id):
        now = int(time.time())
        start = now - 20 * 60
        data = await self._get(f"{CLOB_API}/prices-history", params={"tokenId": token_id, "startTs": start, "endTs": now, "fidelity": 1})
        if not data:
            data = await self._get(f"{CLOB_API}/prices-history", params={"market": market.get("id", ""), "startTs": start, "endTs": now, "fidelity": 1})
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
                bids = book.get("bids") or []
                asks = book.get("asks") or []
                best_bid = float(bids[0]["price"]) if bids else 0
                best_ask = float(asks[0]["price"]) if asks else 0
                if best_bid and best_ask:
                    return (best_bid + best_ask) / 2
            except Exception:
                pass
        return None

    async def get_open_positions(self):
        path = "/positions"
        headers = _l1_headers(self._api_key, self._api_secret, self._api_passphrase, "GET", path)
        try:
            async with self._s.get(f"{CLOB_API}{path}", headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                data = await r.json()
                if not data:
                    return []
                return data if isinstance(data, list) else data.get("data", [])
        except Exception as exc:
            logger.debug("get_open_positions failed: %s", exc)
            return []

    async def get_balance(self) -> float:
        path = "/balance"
        headers = _l1_headers(self._api_key, self._api_secret, self._api_passphrase, "GET", path)
        try:
            async with self._s.get(f"{CLOB_API}{path}", headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                data = await r.json()
                logger.info("Balance API (status %s): %s", r.status, data)
                val = (data.get("balance") or data.get("amount") or
                       data.get("cash") or data.get("availableBalance") or 0)
                return float(val)
        except Exception as exc:
            logger.warning("get_balance failed: %s — skipping balance check", exc)
            return 999.0

    async def place_market_order(self, token_id, side, amount_usd):
        if self._clob_client:
            loop = asyncio.get_event_loop()
            try:
                result = await loop.run_in_executor(None, self._sync_market_order, token_id, side, amount_usd)
                if result:
                    return result
            except Exception as exc:
                logger.warning("CLOB order failed, trying REST: %s", exc)
        return await self._rest_market_order(token_id, side, amount_usd)

    def _sync_market_order(self, token_id, side, amount_usd):
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        try:
            from py_clob_client.clob_types import BUY, SELL
            s = BUY if side == "BUY" else SELL
        except ImportError:
            s = 0 if side == "BUY" else 1
        signed = self._clob_client.create_market_order(MarketOrderArgs(token_id=token_id, amount=amount_usd, side=s))
        resp = self._clob_client.post_order(signed, OrderType.FOK)
        logger.info("CLOB order response: %s", resp)
        return resp

    async def _rest_market_order(self, token_id, side, amount_usd):
        resp = await self._post("/order", {"token_id": token_id, "side": side, "amount": str(amount_usd), "order_type": "MARKET"})
        logger.info("REST order response: %s", resp)
        return resp

    async def close_position(self, token_id, size):
        if self._clob_client:
            loop = asyncio.get_event_loop()
            try:
                return await loop.run_in_executor(None, self._sync_close_position, token_id, size)
            except Exception as exc:
                logger.warning("CLOB close failed, trying REST: %s", exc)
        return await self._rest_market_order(token_id, "SELL", size)

    def _sync_close_position(self, token_id, size):
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        try:
            from py_clob_client.clob_types import SELL
        except ImportError:
            SELL = 1
        signed = self._clob_client.create_market_order(MarketOrderArgs(token_id=token_id, amount=size, side=SELL))
        return self._clob_client.post_order(signed, OrderType.FOK)

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

    def get_event_date(self, market):
        raw = market.get("startDate") or market.get("start_date") or market.get("endDate") or market.get("endDateIso") or market.get("resolutionTime") or ""
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
            end_ts = float(raw) if isinstance(raw, (int, float)) else datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
            return end_ts - time.time()
        except Exception:
            return None

    def market_url(self, market):
        slug = market.get("slug") or market.get("marketSlug") or ""
        if slug:
            return f"https://polymarket.com/event/{slug}"
        mid = market.get("id") or ""
        return f"https://polymarket.com/event/{mid}" if mid else "https://polymarket.com"
