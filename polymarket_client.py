"""
Polymarket API client — market data + trade execution.
Uses polymarket-us SDK for authenticated trading on Polymarket.US.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

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
        self._slug_map: dict[str, str] = {}
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

    # ---------------------------------------------------------------- #
    # Market scanning — SDK first, Gamma fallback                       #
    # ---------------------------------------------------------------- #

    async def get_sports_markets(self, limit=200):
        us_markets = await self._get_us_sdk_markets(limit)
        if us_markets:
            allowed = [m for m in us_markets if self._is_allowed(m)]
            logger.info("Polymarket.US SDK: %d markets, %d allowed", len(us_markets), len(allowed))
            return allowed

        # Fallback to Gamma API
        data = await self._get(
            f"{GAMMA_API}/markets",
            params={"active": "true", "closed": "false", "limit": limit,
                    "order": "volume24hr", "ascending": "false"},
        )
        markets = data if isinstance(data, list) else (data or {}).get("data", []) if data else []
        return [m for m in markets if self._is_allowed(m)]

    async def _get_us_sdk_markets(self, limit=200) -> list[dict]:
        if not self._us_client:
            return []
        loop = asyncio.get_event_loop()

        def _build_markets(events):
            markets = []
            for event in events:
                event_slug = event.get("slug") or event.get("eventSlug") or ""
                sub = event.get("markets") or []
                if sub:
                    for m in sub:
                        row = {**event, **m}
                        row["active"]         = event.get("active", True)
                        row["closed"]         = False
                        row["slug"]           = m.get("slug") or event_slug
                        row["eventSlug"]      = event_slug
                        row["question"]       = m.get("question") or m.get("title") or event.get("title") or ""
                        row["volume24hr"]     = event.get("volume24hr") or m.get("volume24hr") or 0
                        row["resolutionTime"] = m.get("resolutionTime") or event.get("resolutionTime") or ""
                        row["eventState"]     = event.get("eventState") or ""
                        markets.append(row)
                else:
                    event["slug"]     = event_slug
                    event["question"] = event.get("question") or event.get("title") or ""
                    markets.append(event)
            return markets

        def _extract_events(data):
            return (
                data if isinstance(data, list)
                else (data or {}).get("data") or (data or {}).get("events") or (data or {}).get("results") or []
            ) if data else []

        now_ts = time.time()

        def _upcoming_in(markets):
            for m in markets:
                gst = m.get("gameStartTime")
                if not gst:
                    continue
                try:
                    ts = datetime.fromisoformat(str(gst).replace("Z", "+00:00")).timestamp()
                    if ts > now_ts:
                        return True
                except Exception:
                    pass
            return False

        try:
            # Pass 1: try sort by gameStartTime DESC — fast path if API honours it
            data = await loop.run_in_executor(
                None,
                lambda: self._us_client.events.list({
                    "limit": 200,
                    "active": True,
                    "order": "gameStartTime",
                    "ascending": False,
                }),
            )
            events = _extract_events(data)
            if events:
                logger.info("US SDK pass-1: %d events, sample keys: %s", len(events), list(events[0].keys()))
            markets = _build_markets(events)

            if _upcoming_in(markets):
                logger.info("Pass 1 found upcoming games — %d markets total", len(markets))
                return markets

            # Pass 2: API ignores sort; paginate forward until we find upcoming games
            # NBA regular season ~1,230 events; Finals at ~1,300+
            logger.info("Pass 1: no upcoming games found — paginating (200/page) to find them")
            all_markets = list(markets)
            offset = 200
            max_offset = 2000
            found = False
            while offset <= max_offset:
                off = offset  # capture for lambda
                page_data = await loop.run_in_executor(
                    None,
                    lambda: self._us_client.events.list({
                        "limit": 200,
                        "active": True,
                        "offset": off,
                    }),
                )
                page_events = _extract_events(page_data)
                if not page_events:
                    logger.info("Pagination stopped at offset %d — no more events", off)
                    break
                page_markets = _build_markets(page_events)
                all_markets.extend(page_markets)
                logger.info("Paginated offset=%d: +%d markets (%d total)", off, len(page_markets), len(all_markets))
                if _upcoming_in(page_markets):
                    logger.info("Found upcoming games at offset %d", off)
                    found = True
                    # Fetch a few more pages to get all upcoming events
                    for extra_off in range(off + 200, off + 600, 200):
                        eo = extra_off
                        extra_data = await loop.run_in_executor(
                            None,
                            lambda: self._us_client.events.list({
                                "limit": 200,
                                "active": True,
                                "offset": eo,
                            }),
                        )
                        extra_events = _extract_events(extra_data)
                        if not extra_events:
                            break
                        all_markets.extend(_build_markets(extra_events))
                    break
                offset += 200

            if not found:
                logger.warning("Pagination exhausted to offset %d — no upcoming games found", offset)

            game_times = sorted(set(
                m.get("gameStartTime", "")[:10]
                for m in all_markets if m.get("gameStartTime")
            ))
            logger.info("gameStartTime latest dates across %d markets: %s",
                        len(all_markets), game_times[-10:])
            return all_markets
        except Exception as exc:
            logger.warning("US SDK events.list failed: %s — falling back to Gamma API", exc)
            return []

    def _is_allowed(self, market):
        if not market.get("active", True) or market.get("closed", False):
            logger.debug("BLOCKED active/closed: %s", (market.get("question") or market.get("title") or "")[:60])
            return False
        # Skip completed/final games
        event_state_raw = market.get("eventState")
        if event_state_raw and not isinstance(event_state_raw, str):
            event_state = str(event_state_raw.get("status") or event_state_raw.get("state") or "").upper()
        else:
            event_state = str(event_state_raw or "").upper()
        if event_state in ("FINAL", "COMPLETED", "POST_GAME", "POSTGAME", "ENDED", "RESOLVED"):
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
                logger.debug("BLOCKED keyword '%s': %s", kw, text[:80])
                return False
        return True

    # ---------------------------------------------------------------- #
    # Price data                                                        #
    # ---------------------------------------------------------------- #

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

    # ---------------------------------------------------------------- #
    # Account                                                           #
    # ---------------------------------------------------------------- #

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
            logger.warning("get_balance failed: %s — assuming funds available", exc)
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

    # ---------------------------------------------------------------- #
    # Order placement                                                   #
    # ---------------------------------------------------------------- #

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
        order = {
            "marketSlug": market_slug,
            "intent":     intent,
            "type":       "ORDER_TYPE_LIMIT",
            "price":      {"value": str(round(price, 4)), "currency": "USD"},
            "quantity":   quantity,
            "tif":        "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        }
        logger.info("Placing order: %s", order)
        try:
            resp = self._us_client.orders.create(order)
            logger.info("Order response: %s", resp)
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

    # ---------------------------------------------------------------- #
    # Helpers                                                           #
    # ---------------------------------------------------------------- #

    def resolve_token_id(self, market, side):
        tokens = market.get("clobTokenIds") or market.get("tokens") or []
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except Exception:
                tokens = []
        if tokens:
            idx = 0 if side == "YES" else 1
            tok = tokens[idx] if idx < len(tokens) else tokens[0]
            if isinstance(tok, dict):
                return tok.get("token_id") or tok.get("id")
            return str(tok)
        return (
            market.get("conditionId") or market.get("id")
            or market.get("slug") or market.get("eventSlug") or None
        )

    async def get_market_price(self, market: dict, token_id: str) -> float | None:
        question = (market.get("question") or "")[:40]

        # outcomePrices — skip if values are 0/1 (binary markers, not probabilities)
        op = market.get("outcomePrices")
        if op:
            try:
                prices = json.loads(op) if isinstance(op, str) else op
                p = float(prices[0])
                if 0 < p < 1:
                    return p
            except Exception:
                pass

        # marketSides — list of {side, price/probability} dicts
        sides = market.get("marketSides") or []
        logger.debug("marketSides for '%s': %r", question, sides)
        if sides:
            try:
                if isinstance(sides, str):
                    sides = json.loads(sides)
                for s in sides:
                    if isinstance(s, dict):
                        if str(s.get("side") or s.get("outcome") or "").upper() in ("YES", "LONG", "0"):
                            for k in ("price", "probability", "lastPrice", "bestAsk", "bestBid"):
                                raw = s.get(k)
                                if raw is not None:
                                    p = float(raw)
                                    if 0 < p < 1:
                                        return p
                for s in sides:
                    if isinstance(s, dict):
                        for k in ("price", "probability", "lastPrice", "bestAsk", "bestBid"):
                            raw = s.get(k)
                            if raw is not None:
                                try:
                                    p = float(raw)
                                    if 0 < p < 1:
                                        return p
                                except Exception:
                                    pass
            except Exception as exc:
                logger.info("marketSides parse error: %s", exc)

        # outcomes — list or dict
        outcomes = market.get("outcomes") or []
        logger.debug("outcomes for '%s': %r", question, outcomes)
        if outcomes:
            try:
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                items = outcomes if isinstance(outcomes, list) else list(outcomes.values())
                for item in items:
                    if isinstance(item, dict):
                        for k in ("price", "probability", "lastPrice"):
                            raw = item.get(k)
                            if raw is not None:
                                try:
                                    p = float(raw)
                                    if 0 < p < 1:
                                        return p
                                except Exception:
                                    pass
            except Exception as exc:
                logger.info("outcomes parse error: %s", exc)

        for field in ("lastTradePrice", "price", "bestBid"):
            raw = market.get(field)
            if raw:
                try:
                    p = float(raw)
                    if 0 < p < 1:
                        return p
                except Exception:
                    pass

        if token_id and len(str(token_id)) >= 32 and str(token_id).replace("-", "").isalnum():
            return await self.get_current_price(token_id)

        logger.debug("no-price fields for '%s': marketSides=%r outcomes=%r op=%r", question, sides, outcomes, op)
        return None

    def get_market_slug(self, market: dict) -> str:
        return (
            market.get("slug")
            or market.get("marketSlug")
            or market.get("eventSlug")
            or ""
        )

    def get_event_date(self, market):
        raw = (
            market.get("resolutionTime") or market.get("endDate")
            or market.get("startDate") or market.get("endDateIso") or ""
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

    def seconds_to_resolution(self, market):
        raw = market.get("resolutionTime") or market.get("closeTime")
        if not raw:
            raw = market.get("gameStartTime")
            if not raw:
                return None
            try:
                if isinstance(raw, (int, float)):
                    game_ts = float(raw)
                else:
                    game_ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
                return (game_ts + 4 * 3600) - time.time()
            except Exception:
                return None
        try:
            end_ts = (
                float(raw) if isinstance(raw, (int, float))
                else datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
            )
            return end_ts - time.time()
        except Exception:
            return None

    def market_url(self, market):
        slug = market.get("slug") or market.get("marketSlug") or ""
        if slug:
            return f"https://polymarket.us/event/{slug}"
        mid = market.get("id") or ""
        return f"https://polymarket.us/event/{mid}" if mid else "https://polymarket.us"
