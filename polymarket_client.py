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
        self._slug_map: dict[str, str] = {}
        self._upcoming_offset: int = 0  # cached after first scan finds upcoming games
        self._last_markets: list = []   # updated every scan; used by get_token_id_for_slug
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
            self._last_markets = us_markets  # cache for slug→token_id lookups
        allowed = [m for m in us_markets if self._is_allowed(m)]
        logger.info("Polymarket.US SDK: %d markets, %d allowed", len(us_markets), len(allowed))
        return allowed

    def get_token_id_for_slug(self, slug: str, side: str = "YES") -> str | None:
        """Return the CLOB token_id for a slug from the cached market list."""
        for m in self._last_markets:
            if self.get_market_slug(m) == slug:
                tid = self.resolve_token_id(m, side)
                if tid:
                    return tid
        return None

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
            if self._upcoming_offset > 0:
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
        secs = self.seconds_to_resolution(market)
        if secs is not None and secs <= 0:
            logger.debug("BLOCKED resolved(%.1fd ago): %s", abs(secs) / 86400, (market.get("question") or "")[:60])
            return False
        event_state_raw = market.get("eventState")
        if event_state_raw and not isinstance(event_state_raw, str):
            event_state = str(event_state_raw.get("status") or event_state_raw.get("state") or "").upper()
        else:
            event_state = str(event_state_raw or "").upper()
        if event_state in ("FINAL
