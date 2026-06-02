"""
Trigger evaluation and trade decision logic.

Pre-filters (all must pass):
  - Price between 0.05 and 0.95 (not a near-decided market)
  - Minimum 24h volume of $100 (ensures real liquidity)
  - Must resolve within 30 days (eliminates far-future speculation)

4 triggers — at least 2 must fire:
  T1  Price outside 30-70% range (meaningful edge, not coin-flip)
  T2  Price moved >= 3% in last 15 min (momentum / live action)
  T3  24-h volume > 1.5x market daily average (crowd interest spike)
  T4  Resolves within 7 days (active or imminent event)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polymarket_client import PolymarketClient

logger = logging.getLogger(__name__)

# ── Pre-filters ───────────────────────────────────────────────────────────────
MIN_PRICE     = 0.05           # skip near-zero outcomes
MAX_PRICE     = 0.95           # skip near-certain outcomes
MIN_VOL_24H   = 100.0          # minimum $100 24h volume (real liquidity)
MAX_DAYS_OUT  = 30 * 86_400    # must resolve within 30 days

# ── Trigger thresholds ────────────────────────────────────────────────────────
T1_LOW  = 0.30                 # meaningful underdog (5–30¢)
T1_HIGH = 0.70                 # meaningful favourite (70–95¢)
T2_MOVE = 0.03                 # 3% price move in 15 min (lowered to catch more)
T3_MULT = 1.5                  # 24h volume > 1.5× daily avg (lowered to catch more)
T4_SECS = 7 * 86_400           # resolves within 7 days


@dataclass
class TradeSignal:
    market_id:  str
    question:   str
    side:       str
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
    """Run pre-filters then triggers. Return signal if >=2 fire."""

    question  = market.get("question") or market.get("title") or ""
    market_id = market.get("id") or market.get("conditionId") or ""

    token_id = client.resolve_token_id(market, "YES")
    if not token_id:
        return None

    # ── Pre-filter: must resolve within 30 days ───────────────────────────
    secs = client.seconds_to_resolution(market)
    if secs is None or secs <= 0 or secs > MAX_DAYS_OUT:
        return None

    # ── Pre-filter: minimum liquidity ─────────────────────────────────────
    vol_24h = _safe_float(market.get("volume24hr") or market.get("volume24Hour"))
    if vol_24h < MIN_VOL_24H:
        logger.debug("Skipping %s — low volume $%.0f", question[:50], vol_24h)
        return None

    price = await client.get_current_price(token_id)
    if not price:
        return None

    # ── Pre-filter: competitive price range ───────────────────────────────
    if price < MIN_PRICE or price > MAX_PRICE:
        logger.debug("Skipping %s — price %.3f outside range", question[:50], price)
        return None

    triggers: list[str] = []

    # ── T1: meaningful edge ───────────────────────────────────────────────
    if price < T1_LOW or price > T1_HIGH:
        triggers.append(f"T1:prob={price:.2f}")

    # ── T2: price momentum ────────────────────────────────────────────────
    price_15m = await client.get_price_15min_ago(market, token_id)
    if price_15m and price_15m > 0:
        move = abs(price - price_15m) / price_15m
        if move >= T2_MOVE:
            triggers.append(f"T2:move={move:.1%}")

    # ── T3: volume spike ──────────────────────────────────────────────────
    vol_all   = _safe_float(market.get("volume") or market.get("volumeNum"))
    days_est  = max(1.0, _safe_float(market.get("daysAgo"), 30.0))
    daily_avg = vol_all / days_est if vol_all else 0
    if daily_avg > 0 and vol_24h > T3_MULT * daily_avg:
        triggers.append(f"T3:vol24h={vol_24h:.0f}")

    # ── T4: event active or imminent (within 7 days) ──────────────────────
    if 0 < secs <= T4_SECS:
        triggers.append(f"T4:days={secs/86400:.1f}")

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
