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
    # Non-sports / politics
    "politics", "election", "president", "congress", "senate",
    "trump", "harris", "biden", "democrat", "republican",
    "war", "conflict", "invasion", "missile", "nato",
    "fed rate", "interest rate", "inflation", "gdp",
    "oscar", "grammy", "emmy", "celebrity", "reality tv",
    # Esports — not tradeable on Polymarket.US CLOB
    "counter-strike", "cs2", "csgo", "dota", "league of legends", "valorant",
    "esport", "e-sport", "overwatch", "fortnite", "pubg", "apex legends",
    "map 1", "map 2", "map 3", "map 4", "map 5",
    # Spread / handicap / prop markets — CLOB rejects these
    "spread:", "handicap", "(-", "(+",
    # Game segments — only trade full-game markets
    "first half", "second half", "1st half", "2nd half", "halftime", "half time",
    "first quarter", "second quarter", "third quarter", "fourth quarter",
    "1st quarter", "2nd quarter", "3rd quarter", "4th quarter",
    "first period", "second period", "third period",
    # Player props — individual stat lines, not game outcomes
    "rushing yards", "passing yards", "receiving yards",
    "total rebounds", "total assists",
    "strikeouts", "home runs", "hits and runs",
    "anytime scorer", "first scorer", "last scorer",
    "to score 2+", "to score 3+", "to record",
    # Over/under totals — block decimal line markets (e.g. "Under 2.5", "Over 1.5 goals", "O/U 3.5")
    "over 0.", "over 1.", "over 2.", "over 3.", "over 4.", "over 5.",
    "under 0.", "under 1.", "under 2.", "under 3.", "under 4.", "under 5.",
    "o/u", " ou ", "total goals", "total runs", "total sets", "total games",
    # Award / multi-outcome markets — no binary YES/NO CLOB pricing
    "mvp", "most valuable", "award", "golden boot", "ballon d'or",
    # Exact score markets — specific scoreline props, not binary outcomes
    "exact score", "correct score", "scoreline",
    # Weather / temperature markets — tc-temp-* slugs, Miami/NYC/LA daily high, etc.
    "temperature", "temp ", "weather", "°f", "°c", "humidity",
    "rainfall", "precipitation", "tc-temp", "daily high", "daily low",
    "heat index", "wind speed", "snowfall",
    # Crypto / financial markets
    "bitcoin", "ethereum", "btc", "eth", "crypto", "stock", "nasdaq",
    "s&p", "fed funds", "treasury",
}

# At least one of these must appear in question/title/category/tags for a market to be tradeable.
# Blocks geopolitical, tech, entertainment, and other non-sports markets.
_SPORT_REQUIRED = {
    # Strong match/contest signals
    "vs", "v.", " vs.", "match", "fight", "bout",
    "tournament", "championship", "playoff", "series",
    # Sports by name
    "soccer", "football", "nfl", "nba", "nhl", "mlb",
    "ufc", "mma", "boxing", "wrestling",
    "tennis", "golf", "f1", "formula 1", "indycar",
    "rugby", "cricket", "hockey", "baseball", "basketball",
    "afl", "aussie rules",
    # Competitions / leagues
    "world cup", "champions league", "europa league", "premier league",
    "la liga", "serie a", "bundesliga", "ligue 1", "mls",
    "copa america", "grand slam", "wimbledon", "french open", "australian open",
    "super bowl", "world series", "stanley cup", "nba finals",
    "grand prix", "open championship",
    "gold cup", "nations league",
    # Sport-specific outcome terms
    "goal", "inning", "knockout", "ko", "tko", "submission", "decision",
}


class PolymarketClient:
    def __init__(self, session, key_id: str, secret_key: str) -> None:
        self._s          = session
        self._session    = session
        self._key_id     = key_id.strip()
        self._secret_key = secret_key.strip()
        self._us_client  = self._init_us_client()
        self._market_cache: dict = {}
        self._slug_map: dict[str, str] = {}
        self._upcoming_offset: int = self._load_offset_cache()
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

    _OFFSET_CACHE_FILE = "upcoming_offset.json"

    def _load_offset_cache(self) -> int:
        try:
            import json, pathlib
            p = pathlib.Path(self._OFFSET_CACHE_FILE)
            if p.exists():
                val = json.loads(p.read_text()).get("upcoming_offset", 0)
                logger.info("Loaded cached upcoming_offset=%d", val)
                return int(val)
        except Exception:
            pass
        return 0

    def _save_offset_cache(self, offset: int) -> None:
        try:
            import json, pathlib
            pathlib.Path(self._OFFSET_CACHE_FILE).write_text(
                json.dumps({"upcoming_offset": offset})
            )
        except Exception:
            pass

    async def _get(self, url, params=None):
        try:
            async with self._s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                r.raise_for_status()
                return await r.json()
        except Exception as exc:
            logger.debug("GET %s failed: %s", url, exc)
            return None

    # ---------------------------------------------------------------- #
    # Market scanning — Polymarket.US SDK only (no Gamma/COM fallback) #
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

        _market_keys_logged = False

        def _build_markets(events):
            nonlocal _market_keys_logged
            markets = []
            for event in events:
                event_slug = event.get("slug") or event.get("eventSlug") or ""
                sub = event.get("markets") or []
                if sub:
                    for m in sub:
                        if not _market_keys_logged:
                            _market_keys_logged = True
                            logger.info(
                                "MARKET-KEYS diag: event.endDate=%s event.startTime=%s "
                                "market keys=%s",
                                event.get("endDate"), event.get("startTime"),
                                list(m.keys()),
                            )
                        row = {**event, **m}
                        row["active"]         = event.get("active", True)
                        row["closed"]         = False
                        row["slug"]           = m.get("slug") or event_slug
                        row["eventSlug"]      = event_slug
                        row["question"]       = m.get("question") or m.get("title") or event.get("title") or ""
                        row["volume24hr"]     = event.get("volume24hr") or m.get("volume24hr") or 0
                        row["resolutionTime"] = (
                            m.get("resolutionTime") or m.get("closeTime") or m.get("closedTime") or
                            event.get("resolutionTime") or event.get("closeTime") or event.get("closedTime") or
                            event.get("endDate") or m.get("endDate") or ""
                        )
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
            window = now_ts + 7 * 86_400
            for m in markets:
                raw = m.get("gameStartTime")
                if not raw:
                    continue
                try:
                    if isinstance(raw, (int, float)):
                        ts = float(raw)
                    else:
                        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
                    if now_ts < ts < window:
                        logger.debug("_upcoming_in: gameStartTime=%s in window", raw)
                        return True
                except Exception:
                    pass
            return False

        async def _fetch_page(off: int) -> list:
            o = off
            try:
                data = await loop.run_in_executor(
                    None,
                    lambda: self._us_client.events.list({
                        "limit": 200,
                        "active": True,
                        "offset": o,
                    }),
                )
                return _extract_events(data)
            except Exception as exc:
                logger.debug("_fetch_page offset=%d failed: %.120s", o, str(exc))
                return []

        try:
            all_markets: list[dict] = []

            for offset in range(0, 32_000, 200):
                page_events = await _fetch_page(offset)
                if not page_events:
                    logger.info("Scan stopped at offset %d — %d markets collected", offset, len(all_markets))
                    break
                page_markets = _build_markets(page_events)
                all_markets.extend(page_markets)
                if offset == 0:
                    logger.info("SDK page offset=0: %d events, sample keys: %s",
                                len(page_events), list(page_events[0].keys()))
                else:
                    logger.info("Scan offset=%d: +%d markets (%d total)",
                                offset, len(page_markets), len(all_markets))

            game_times = sorted(set(
                m.get("gameStartTime", "")[:10]
                for m in all_markets if m.get("gameStartTime")
            ))
            logger.info("gameStartTime latest dates across %d markets: %s",
                        len(all_markets), game_times[-10:])
            return all_markets
        except Exception as exc:
            logger.warning("US SDK events.list failed: %s", exc)
            return []

    def _is_allowed(self, market):
        if not market.get("active", True) or market.get("closed", False):
            logger.debug("BLOCKED active/closed: %s", (market.get("question") or market.get("title") or "")[:60])
            return False
        event_state_raw = market.get("eventState")
        if event_state_raw and not isinstance(event_state_raw, str):
            event_state = str(event_state_raw.get("status") or event_state_raw.get("state") or "").upper()
        else:
            event_state = str(event_state_raw or "").upper()
        if event_state in ("FINAL", "COMPLETED", "POST_GAME", "POSTGAME", "ENDED", "RESOLVED"):
            return False

        game_raw = market.get("gameStartTime")
        if game_raw:
            try:
                if isinstance(game_raw, (int, float)):
                    game_ts = float(game_raw)
                else:
                    game_ts = datetime.fromisoformat(str(game_raw).replace("Z", "+00:00")).timestamp()
                now_ts = time.time()
                if game_ts > now_ts + 60 * 86400:
                    logger.debug("BLOCKED far-future: %s", (market.get("question") or "")[:60])
                    return False
            except Exception:
                pass

        text = " ".join([
            (market.get("question") or "").lower(),
            (market.get("title") or "").lower(),
            (market.get("category") or "").lower(),
            (market.get("slug") or "").lower(),
            (market.get("eventSlug") or "").lower(),
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

        # Require at least one sport signal — blocks geopolitical/tech/entertainment markets
        has_game_data = bool(
            market.get("gameStartTime") or
            market.get("teams") or
            market.get("sportradarGameId") or
            market.get("sportradarEventId")
        )
        if not has_game_data and not any(kw in text for kw in _SPORT_REQUIRED):
            logger.debug("BLOCKED non-sport: %s", text[:80])
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
        if not token_id or len(str(token_id)) < 32:
            return None
        # Reject slugs (contain letters+dashes) — only real hex/numeric token IDs are valid
        tok = str(token_id)
        if not tok.replace("-", "").isdigit() and not (len(tok) >= 60 and tok.replace("0x", "").replace("-", "").isalnum()):
            # Accept only long hex-like IDs (real CLOB token IDs are 64+ char hex)
            if len(tok) < 60 or "-" in tok:
                return None
        data = await self._get(f"{CLOB_API}/last-trade-price", params={"token_id": token_id})
        if data:
            try:
                p = float(data.get("price") or 0)
                if 0.01 < p < 0.99:
                    return p
            except Exception:
                pass
        return None

    async def get_price_by_slug(self, market_slug: str) -> float | None:
        """Fetch live YES-side price by market slug — tries US SDK, gateway, then Gamma."""
        # Use cached token ID if we already resolved it
        cached = self._slug_map.get(market_slug)
        if cached and len(cached) >= 60:
            price = await self.get_current_price(cached)
            if price:
                return price

        # Try US SDK first — same authenticated source as market scanning
        if self._us_client:
            try:
                loop = asyncio.get_event_loop()
                data = await loop.run_in_executor(
                    None,
                    lambda: self._us_client.events.list({"slug": market_slug, "limit": 5})
                )
                events = (
                    data if isinstance(data, list)
                    else (data or {}).get("data") or (data or {}).get("events") or []
                ) if data else []
                for event in events:
                    event_slug = event.get("slug") or event.get("eventSlug") or ""
                    sub = event.get("markets") or []
                    for m in ([event] + sub):
                        m_slug = m.get("slug") or ""
                        if m_slug != market_slug and event_slug != market_slug:
                            continue
                        token_id = self.resolve_token_id(m, "YES")
                        if token_id and len(str(token_id)) >= 60:
                            self._slug_map[market_slug] = str(token_id)
                            clob = await self.get_current_price(token_id)
                            if clob:
                                logger.info("get_price_by_slug %s [US SDK CLOB]: %.3f", market_slug, clob)
                                return clob
                        price = await self.get_market_price(m, token_id)
                        if price and abs(price - 0.5) > 0.01:
                            logger.info("get_price_by_slug %s [US SDK]: YES=%.3f", market_slug, price)
                            return price
            except Exception as exc:
                logger.debug("get_price_by_slug US SDK failed for %s: %s", market_slug, exc)

        # HTTP fallback: US gateway events, US gateway markets, Gamma
        candidates = [
            ("https://gateway.polymarket.us/v1/events", {"slug": market_slug, "limit": 1}),
            ("https://gateway.polymarket.us/v1/markets", {"slug": market_slug, "limit": 1}),
            (f"{GAMMA_API}/markets", {"slug": market_slug, "limit": 1}),
        ]
        for url, params in candidates:
            data = await self._get(url, params=params)
            if not data:
                continue
            items = (
                data if isinstance(data, list)
                else data.get("data") or data.get("events") or data.get("markets") or []
            )
            if not items:
                continue
            item = items[0]
            # Events embed sub-markets; check both the event itself and its markets array
            sub = item.get("markets") or []
            for m in ([item] + sub):
                slug_match = m.get("slug") == market_slug or not sub
                if not slug_match:
                    continue
                token_id = self.resolve_token_id(m, "YES")
                if token_id and len(str(token_id)) >= 60:
                    self._slug_map[market_slug] = str(token_id)
                    clob = await self.get_current_price(token_id)
                    if clob:
                        logger.info("get_price_by_slug %s [%s]: CLOB YES=%.3f", market_slug, url, clob)
                        return clob
                price = await self.get_market_price(m, token_id)
                if price and abs(price - 0.5) > 0.02:
                    logger.info("get_price_by_slug %s [%s]: market_price YES=%.3f", market_slug, url, price)
                    return price

        return None

    # ---------------------------------------------------------------- #
    # Account                                                           #
    # ---------------------------------------------------------------- #

    async def get_balance(self) -> float:
        if not self._us_client:
            logger.warning("get_balance: no Polymarket.US client — returning 0")
            return 0.0
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
            return 0.0
        except Exception as exc:
            logger.warning("get_balance failed: %s — returning 0 to prevent unsafe trades", exc)
            return 0.0

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
            positions = data.get("positions", {})
            if isinstance(positions, dict):
                result = []
                for slug, pos in positions.items():
                    if isinstance(pos, dict):
                        pos.setdefault("marketSlug", slug)
                        result.append(pos)
                logger.info("get_open_positions: %d positions from portfolio", len(result))
                return result
            return positions if isinstance(positions, list) else []
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
        intent = "ORDER_INTENT_BUY_LONG" if side == "YES" else "ORDER_INTENT_BUY_SHORT"
        order_price = round(price, 4) if side == "YES" else round(1.0 - price, 4)
        order_price = max(0.01, min(0.99, order_price))
        quantity = max(1, round(amount_usd / order_price))
        order = {
            "marketSlug": market_slug,
            "intent":     intent,
            "type":       "ORDER_TYPE_LIMIT",
            "price":      {"value": str(order_price), "currency": "USD"},
            "quantity":   quantity,
            "tif":        "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        }
        logger.info("Placing order: %s", order)
        try:
            resp = self._us_client.orders.create(order)
            logger.info("Order response: %s", resp)
            return resp
        except AuthenticationError as exc:
            logger.error("Auth error placing order for %s: %s", market_slug, exc)
        except BadRequestError as exc:
            logger.error("Bad request placing order for %s: %s | payload=%s", market_slug, exc, order)
        except NotFoundError as exc:
            logger.error("Market not found (%s): %s", market_slug, exc)
        except Exception as exc:
            logger.error("Order error (%s) for %s: %s | payload=%s", type(exc).__name__, market_slug, exc, order)
        return None

    async def close_position(
        self, market_slug: str, side: str, price: float, size_usd: float
    ) -> dict | None:
        close_side = "NO" if side == "YES" else "YES"
        if close_side == "YES":
            yes_equiv = 1.0 - price
            aggressive_price = min(round(yes_equiv + 0.10, 4), 0.95)
        else:
            aggressive_price = max(round(price - 0.10, 4), 0.05)
        logger.info(
            "close_position: side=%s price=%.3f close_side=%s aggressive=%.3f",
            side, price, close_side, aggressive_price,
        )
        return await self.place_market_order(market_slug, close_side, aggressive_price, size_usd)

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

        op = market.get("outcomePrices")
        if op:
            try:
                prices = json.loads(op) if isinstance(op, str) else op
                p = float(prices[0])
                if 0 < p < 1:
                    return p
            except Exception:
                pass

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
        raw = (
            market.get("resolutionTime") or market.get("closeTime") or market.get("closedTime") or
            market.get("endDate")
        )
        if not raw:
            for gst_key in ("gameStartTime", "startTime"):
                game_raw = market.get(gst_key)
                if not game_raw:
                    continue
                try:
                    if isinstance(game_raw, (int, float)):
                        game_ts = float(game_raw)
                    else:
                        game_ts = datetime.fromisoformat(str(game_raw).replace("Z", "+00:00")).timestamp()
                    return (game_ts + 4 * 3600) - time.time()
                except Exception:
                    pass
            raw = market.get("startDate")
            if not raw:
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
