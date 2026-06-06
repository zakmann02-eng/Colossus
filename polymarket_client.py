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
    "cpi", "consumer price", "pce", "unemployment", "payroll", "fomc",
    "oscar", "grammy", "emmy", "celebrity", "reality tv",
    "crypto", "bitcoin", "ethereum", "btc", "eth",
}


class PolymarketClient:
    def __init__(self, session, key_id: str, secret_key: str) -> None:
        self._s          = session
        self._key_id     = key_id.strip()
        self._secret_key = secret_key.strip()
        self._us_client  = self._init_us_client()
        self._market_cache: dict = {}
        self._slug_map: dict[str, str] = {}
        self._upcoming_offset: int = 0  # cached after first scan finds upcoming games
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
        allowed = [m for m in us_markets if self._is_allowed(m)]
        logger.info("Polymarket.US SDK: %d markets, %d allowed", len(us_markets), len(allowed))
        return allowed

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

        def _parse_ts(raw) -> float:
            """Parse timestamp. Date-only strings (YYYY-MM-DD) are treated as end-of-day UTC."""
            if raw is None:
                return 0.0
            try:
                ts = float(raw)
                if ts > 1_000_000_000:
                    return ts
            except (TypeError, ValueError):
                pass
            s = str(raw).strip()
            try:
                if "T" in s or len(s) > 10:
                    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
                # Date-only: use 23:59:59 UTC so same-day games aren't falsely excluded
                return datetime.fromisoformat(s + "T23:59:59+00:00").timestamp()
            except Exception:
                return 0.0

        def _fmt_ts(ts: float) -> str:
            if ts <= 0:
                return "no-date"
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

        async def _fetch_page(params: dict) -> list[dict]:
            data = await loop.run_in_executor(
                None,
                lambda p=params: self._us_client.events.list(p),
            )
            return _build_markets(_extract_events(data))

        try:
            # API has ~16k events sorted oldest-first. Fetch from offset 13000 onwards
            # to get the last ~3k events (roughly the past 2 weeks). The analyzer's
            # seconds_to_resolution filter handles time-based pruning — no need to
            # pre-filter for "upcoming" here.
            if self._upcoming_offset > 0:
                # Cache stores end-of-pagination; start 2000 events back to catch new additions
                start_offset = max(0, self._upcoming_offset - 2000)
                logger.info("Resuming from cached end offset %d (start=%d)",
                            self._upcoming_offset, start_offset)
            else:
                start_offset = 13000

            all_markets: list[dict] = []
            for offset in range(start_offset, start_offset + 6000, 200):
                try:
                    page = await _fetch_page({"limit": 200, "offset": offset})
                except Exception as exc:
                    logger.debug("Offset %d failed: %s", offset, exc)
                    continue
                if not page:
                    logger.info("Pagination ended at offset %d — caching", offset)
                    self._upcoming_offset = offset
                    break
                dates = sorted(_parse_ts(m.get("resolutionTime") or m.get("gameStartTime") or "")
                               for m in page)
                dates = [d for d in dates if d > 0]
                rng   = f"{_fmt_ts(dates[0])} → {_fmt_ts(dates[-1])}" if dates else "no dates"
                logger.info("Offset %d: %d events, dates %s", offset, len(page), rng)
                all_markets.extend(page)

            # Log a sample event on first run to aid field diagnostics
            if all_markets and self._upcoming_offset == 0:
                sample = all_markets[-1]
                logger.info("Sample event fields: %s", {
                    k: sample.get(k) for k in (
                        "question", "slug", "active", "eventState",
                        "resolutionTime", "gameStartTime", "closeTime",
                    )
                })

            logger.info("Fetched %d recent markets from offset %d onwards",
                        len(all_markets), start_offset)
            return all_markets

        except Exception as exc:
            logger.warning("US SDK events.list failed: %s", exc)
            return []

    def _is_allowed(self, market):
        if not market.get("active", True) or market.get("closed", False):
            logger.debug("BLOCKED active/closed: %s", (market.get("question") or market.get("title") or "")[:60])
            return False
        # Pre-filter already-resolved markets before volume sort can promote them
        secs = self.seconds_to_resolution(market)
        if secs is not None and secs <= 0:
            logger.debug("BLOCKED resolved(%.1fd ago): %s", abs(secs) / 86400, (market.get("question") or "")[:60])
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

    async def has_liquidity(self, token_id: str, min_usd: float = 0.10) -> bool:
        """Return True if the market has tradeable depth.

        Polymarket.US runs its own order book independent of clob.polymarket.com.
        We only use the CLOB as a hint when the token looks like a real CLOB hex ID;
        otherwise assume liquidity exists and let the order attempt proceed.
        A GTC order that finds no counterpart sits unfilled — no capital lost.
        """
        tid = str(token_id)
        # Only query CLOB for proper hex token IDs (32+ chars); slugs/condition IDs
        # are Polymarket.US-only and the CLOB will return empty books for them.
        if len(tid) < 32 or not tid.replace("-", "").isalnum():
            return True
        book = await self._get(f"{CLOB_API}/book", params={"token_id": token_id})
        if not book:
            return True
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            logger.debug("Empty CLOB book for token %s… — assuming US liquidity", tid[:12])
            return True
        # Both sides present — verify minimum depth
        ask_depth = sum(float(a.get("size", 0)) for a in asks[:3])
        bid_depth = sum(float(b.get("size", 0)) for b in bids[:3])
        if ask_depth < min_usd or bid_depth < min_usd:
            logger.debug(
                "Thin CLOB book for token %s… ask=%.2f bid=%.2f",
                tid[:12], ask_depth, bid_depth,
            )
            return False
        return True

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
            return 999.0  # API error: don't block trading, let order attempt reveal true state

    async def get_open_positions(self):
        if not self._us_client:
            return []
        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(None, self._us_client.portfolio.positions)
            if not data:
                return []
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for key in ("positions", "data", "items", "portfolio", "results"):
                    if key not in data:
                        continue
                    val = data[key]
                    if isinstance(val, list):
                        return val
                    if val is None:
                        return []  # key present but null → genuinely no positions
                logger.warning("get_open_positions: unrecognised response shape — keys: %s", list(data.keys()))
            return []
        except Exception as exc:
            logger.warning("get_open_positions failed: %s", exc)
            return []

    # ---------------------------------------------------------------- #
    # Order placement                                                   #
    # ---------------------------------------------------------------- #

    async def cancel_order(self, order_id: str) -> bool:
        if not self._us_client or not order_id:
            return False
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None, lambda: self._us_client.orders.cancel(order_id)
            )
            logger.info("Cancelled pending order %s", order_id)
            return True
        except Exception as exc:
            logger.warning("Failed to cancel order %s: %s", order_id, exc)
            return False

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
        intent = "ORDER_INTENT_BUY_LONG" if side == "YES" else "ORDER_INTENT_BUY_SHORT"
        # Add 2-cent buffer to cross the spread and get immediate fills.
        # For YES: pay slightly more. For NO (BUY_SHORT): accept slightly lower YES price
        # (equivalent to paying slightly more for NO).
        if side == "YES":
            order_price = round(min(price + 0.02, 0.97), 4)
        else:
            order_price = round(max(price - 0.02, 0.03), 4)
        quantity = max(1, round(amount_usd / order_price))
        order = {
            "marketSlug": market_slug,
            "intent":     intent,
            "type":       "ORDER_TYPE_LIMIT",
            "price":      {"value": str(order_price), "currency": "USD"},
            "quantity":   quantity,
            "tif":        "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
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
        if not self._us_client:
            logger.error("Polymarket.US client not initialised — cannot close position")
            return None
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None, self._sync_close_position, market_slug, side, price, size_usd
            )
        except Exception as exc:
            logger.error("close_position failed for %s: %s", market_slug, exc)
            return None

    def _sync_close_position(
        self, market_slug: str, side: str, price: float, size_usd: float
    ) -> dict | None:
        from polymarket_us import AuthenticationError, BadRequestError, NotFoundError
        quantity = max(1, round(size_usd / price))

        # Try sell intent first (exit long/short), fall back to buying the opposite side
        for intent in (
            "ORDER_INTENT_SELL_LONG" if side == "YES" else "ORDER_INTENT_SELL_SHORT",
            "ORDER_INTENT_BUY_SHORT" if side == "YES" else "ORDER_INTENT_BUY_LONG",
        ):
            order = {
                "marketSlug": market_slug,
                "intent":     intent,
                "type":       "ORDER_TYPE_LIMIT",
                "price":      {"value": str(round(price, 4)), "currency": "USD"},
                "quantity":   quantity,
                "tif":        "TIME_IN_FORCE_GOOD_TILL_CANCEL",
            }
            logger.info("Closing position (intent=%s): %s", intent, order)
            try:
                resp = self._us_client.orders.create(order)
                logger.info("Close response: %s", resp)
                return resp
            except BadRequestError as exc:
                logger.warning("Close intent %s rejected for %s: %s — trying fallback", intent, market_slug, exc)
            except AuthenticationError as exc:
                logger.error("Auth error closing %s: %s", market_slug, exc)
                return None
            except NotFoundError as exc:
                logger.error("Market not found closing %s: %s", market_slug, exc)
                return None
            except Exception as exc:
                logger.error("Close order error for %s (intent=%s): %s", market_slug, intent, exc)
        return None

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
                        # look for YES side price
                        if str(s.get("side") or s.get("outcome") or "").upper() in ("YES", "LONG", "0"):
                            for k in ("price", "probability", "lastPrice", "bestAsk", "bestBid"):
                                raw = s.get(k)
                                if raw is not None:
                                    p = float(raw)
                                    if 0 < p < 1:
                                        return p
                # fallback: just grab first numeric price in range
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
        # Prefer the market-level slug (e.g. 'aec-lol-dk-bro-2026-06-06') over the
        # event slug ('lol-dk-bro-2026-06-06') so it matches the portfolio API exactly.
        candidates = [
            market.get("marketSlug"),
            market.get("slug"),
            market.get("eventSlug"),
        ]
        # Pick the longest non-empty candidate — market slugs are longer than event slugs
        slugs = [s for s in candidates if s]
        return max(slugs, key=len) if slugs else ""

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
                game_ts = (float(raw) if isinstance(raw, (int, float))
                           else datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp())
                return (game_ts + 4 * 3600) - time.time()
            except Exception:
                return None
        try:
            if isinstance(raw, (int, float)):
                end_ts = float(raw)
            else:
                s = str(raw).strip()
                if "T" in s or len(s) > 10:
                    end_ts = datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
                else:
                    # Date-only (YYYY-MM-DD): treat as end-of-day UTC so today's evening
                    # games aren't falsely excluded (midnight UTC is already past by game time)
                    end_ts = datetime.fromisoformat(s + "T23:59:59+00:00").timestamp()
            return end_ts - time.time()
        except Exception:
            return None

    def market_url(self, market):
        slug = market.get("slug") or market.get("marketSlug") or ""
        if slug:
            return f"https://polymarket.us/event/{slug}"
        mid = market.get("id") or ""
        return f"https://polymarket.us/event/{mid}" if mid else "https://polymarket.us"
