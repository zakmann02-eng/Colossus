"""
Trigger evaluation and trade decision logic.

4 triggers — at least 2 must fire:
  T1  YES probability < 20% or > 80%
  T2  Price moved ≥ 5% in last 15 min
  T3  24-h volume > 2× market daily average
  T4  Resolution within 24 hours
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polymarket_client import PolymarketClient

logger = logging.getLogger(__name__)

T1_LOW  = 0.20
T1_HIGH = 0.80
T2_MOVE = 0.05   # 5 %
T3_MULT = 2.0    # 2× average
T4_SECS = 86_400 # 24 h


@dataclass
class TradeSignal:
    market_id:   str
    question:    str
    side:        str          # "YES" or "NO"
    token_id:    str
    price_now:   float
    triggers:    list[str]    = field(default_factory=list)
    score:       int          = 0
    event_date:  str          = "N/A"


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _decide_side(price: float) -> str:
    """Trade the underdog — fade extreme probabilities."""
    return "NO" if price > T1_HIGH else "YES"


async def evaluate_market(
    market: dict,
    client: "PolymarketClient",
) -> TradeSignal | None:
    """Run all 4 triggers against one market. Return signal if ≥2 fire."""

    question  = market.get("question") or market.get("title") or ""
    market_id = market.get("id") or market.get("conditionId") or ""

    # Need YES token to get pricing
    token_id = client.resolve_token_id(market, "YES")
    if not token_id:
        return None

    price = await client.get_current_price(token_id)
    if not price or price <= 0.001:
        return None

    triggers: list[str] = []

    # ── T1: extreme probability ──────────────────────────────────────────
    if price < T1_LOW or price > T1_HIGH:
        triggers.append(f"T1:prob={price:.2f}")

    # ── T2: 15-min price movement ────────────────────────────────────────
    price_15m = await client.get_price_15min_ago(market, token_id)
    if price_15m and price_15m > 0:
        move = abs(price - price_15m) / price_15m
        if move >= T2_MOVE:
            triggers.append(f"T2:move={move:.1%}")

    # ── T3: 24-h volume spike ────────────────────────────────────────────
    vol_24h = _safe_float(market.get("volume24hr") or market.get("volume24Hour"))
    vol_all = _safe_float(market.get("volume") or market.get("volumeNum"))
    # Estimate daily average from total volume (assume market is ~30 days old on average)
    days_est = max(1.0, _safe_float(market.get("daysAgo"), 30.0))
    daily_avg = vol_all / days_est if vol_all else 0
    if daily_avg > 0 and vol_24h > T3_MULT * daily_avg:
        triggers.append(f"T3:vol24h={vol_24h:.0f}")

    # ── T4: resolution within 24 h ───────────────────────────────────────
    secs = client.seconds_to_resolution(market)
    if secs is not None and 0 < secs <= T4_SECS:
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
