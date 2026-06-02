"""
Trigger evaluation and trade decision logic.

Pre-filters (all must pass):
  - Price between 0.05 and 0.95 (not a near-decided market)
  - Resolves within 7 days (event is active or imminent)

4 triggers — at least 2 must fire:
  T1  YES probability between 5–30% or 70–95% (meaningful edge)
  T2  Price moved >= 5% in last 15 min (momentum)
  T3  24-h volume > 2x market daily average (crowd interest)
  T4  Resolution within 24 hours (game is today)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polymarket_client import PolymarketClient

logger = logging.getLogger(__name__)

# Pre-filters
MIN_PRICE    = 0.05          # ignore markets priced below 5¢ (near-decided)
MAX_PRICE    = 0.95          # ignore markets priced above 95¢ (near-decided)
MAX_SECS_OUT = 7 * 86_400   # ignore events more than 7 days away

# Trigger thresholds
T1_LOW  = 0.30               # underdog YES (5–30¢ range)
T1_HIGH = 0.70               # favourite NO (70–95¢ range)
T2_MOVE = 0.05               # 5% price move in 15 min
T3_MULT = 2.0                # 24h volume > 2× daily average
T4_SECS = 86_400             # resolves within 24 h


@dataclass
class TradeSignal:
    market_id:  str
    question:   str
    side:       str           # "YES" or "NO"
    token_id:   str
    price_now:  float
    triggers:   list[str]    = field(default_factory=list)
    score:      int          = 0
    event_date: str          = "N/A"


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _decide_side(price: float) -> str:
    return "NO" if price > T1_HIGH else "YES"


async def evaluate_market(
    market: dict,
    client: "PolymarketClient",
) -> TradeSignal | None:
    """Run pre-filters then all 4 triggers. Return signal if >=2 fire."""

    question  = market.get("question") or market.get("title") or ""
    market_id = market.get("id") or market.get("conditionId") or ""

    token_id = client.resolve_token_id(market, "YES")
    if not token_id:
        return None

    # ── Pre-filter 1: event must be active or within 7 days ─────────────
    secs = client.seconds_to_resolution(market)
    if secs is None or secs <= 0 or secs > MAX_SECS_OUT:
        return None

    price = await client.get_current_price(token_id)
    if not price:
        return None

    # ── Pre-filter 2: price must be in competitive range ─────────────────
    if price < MIN_PRICE or price > MAX_PRICE:
        logger.debug("Skipping %s — price %.3f outside competitive range", question[:50], price)
        return None

    triggers: list[str] = []

    # ── T1: meaningful edge (underdog or favourite range) ────────────────
    if price < T1_LOW or price > T1_HIGH:
        triggers.append(f"T1:prob={price:.2f}")

    # ── T2: 15-min price momentum ────────────────────────────────────────
    price_15m = await client.get_price_15min_ago(market, token_id)
    if price_15m and price_15m > 0:
        move = abs(price - price_15m) / price_15m
        if move >= T2_MOVE:
            triggers.append(f"T2:move={move:.1%}")

    # ── T3: 24-h volume spike ────────────────────────────────────────────
    vol_24h   = _safe_float(market.get("volume24hr") or market.get("volume24Hour"))
    vol_all   = _safe_float(market.get("volume") or market.get("volumeNum"))
    days_est  = max(1.0, _safe_float(market.get("daysAgo"), 30.0))
    daily_avg = vol_all / days_est if vol_all else 0
    if daily_avg > 0 and vol_24h > T3_MULT * daily_avg:
        triggers.append(f"T3:vol24h={vol_24h:.0f}")

    # ── T4: game is today ────────────────────────────────────────────────
    if 0 < secs <= T4_SECS:
        triggers.append(f"T4:secs={secs:.0f}")

    if len(triggers) < 2:
        return None

    side  = _decide_side(price)
    score = min(100, 50 + len(triggers) * 15)

    signal = TradeSignal(
        market_id  = market_id,
        question   = question,
        side       = side,
        token_id   = token_id if side == "YES" else (client.resolve_token_id(market, "NO") or token_id),
        price_now  = price,
        triggers   = triggers,
        score      = score,
        event_date = client.get_event_date(market),
    )

    logger.info(
        "Signal: %s | side=%s price=%.2f triggers=%s score=%d",
        question[:60], side, price, triggers, score,
    )
    return signal
