"""
Trigger evaluation and trade decision logic.

Pre-filters (all must pass):
  - Price between 0.05 and 0.95
  - Minimum $100 24h volume
  - Must resolve within 7 days (weekly/daily trading)

Any 1 trigger fires a trade:
  T1  Price outside 48-52% range
  T2  Price moved >= 1% in last 15 min
  T3  24h volume > 1.5x daily average
  T4  Resolves within 7 days (always fires for near-term markets)

Position sizing by triggers fired:
  1 trigger  → LOW  → $0.10–$0.50  · TP 8%  · SL 8%
  2 triggers → MED  → $0.50–$1.25  · TP 12% · SL 10%
  3+ triggers→ HIGH → $1.25–$2.00  · TP 15% · SL 10%
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polymarket_client import PolymarketClient

logger = logging.getLogger(__name__)

MIN_PRICE    = 0.05
MAX_PRICE    = 0.95
MIN_VOL_24H  = 100.0
MAX_DAYS_OUT = 7 * 86_400

T1_LOW  = 0.48
T1_HIGH = 0.52
T2_MOVE = 0.01
T3_MULT = 1.5
T4_SECS = 7 * 86_400

_TIERS = {
    1: {"label": "LOW",  "min_usd": 0.10, "max_usd": 0.50, "tp": 0.08, "sl": 0.08},
    2: {"label": "MED",  "min_usd": 0.50, "max_usd": 1.25, "tp": 0.12, "sl": 0.10},
    3: {"label": "HIGH", "min_usd": 1.25, "max_usd": 2.00, "tp": 0.15, "sl": 0.10},
}


@dataclass
class TradeSignal:
    market_id:  str
    question:   str
    side:       str
    token_id:   str
    price_now:  float
    triggers:   list[str] = field(default_factory=list)
    score:      int       = 0
    event_date: str       = "N/A"
    amount_usd: float     = 0.50
    tp_pct:     float     = 0.08
    sl_pct:     float     = 0.08
    conviction: str       = "LOW"


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _decide_side(price: float) -> str:
    return "NO" if price > 0.50 else "YES"


def _size_position(n_triggers: int) -> tuple[float, float, float, str]:
    tier = _TIERS.get(n_triggers) or _TIERS[3]
    return round(random.uniform(tier["min_usd"], tier["max_usd"]), 2), tier["tp"], tier["sl"], tier["label"]


async def evaluate_market(market: dict, client: "PolymarketClient") -> TradeSignal | None:

    question  = market.get("question") or market.get("title") or ""
    market_id = market.get("id") or market.get("conditionId") or ""

    token_id = client.resolve_token_id(market, "YES")
    if not token_id:
        return None

    secs = client.seconds_to_resolution(market)
    if secs is None or secs <= 0 or secs > MAX_DAYS_OUT:
        return None

    vol_24h = _safe_float(market.get("volume24hr") or market.get("volume24Hour"))
    if vol_24h < MIN_VOL_24H:
        return None

    price = await client.get_current_price(token_id)
    if not price:
        return None

    if price < MIN_PRICE or price > MAX_PRICE:
        return None

    triggers: list[str] = []

    if price < T1_LOW or price > T1_HIGH:
        triggers.append(f"T1:prob={price:.2f}")

    price_15m = await client.get_price_15min_ago(market, token_id)
    if price_15m and price_15m > 0:
        move = abs(price - price_15m) / price_15m
        if move >= T2_MOVE:
            triggers.append(f"T2:move={move:.1%}")

    vol_all  = _safe_float(market.get("volume") or market.get("volumeNum"))
    days_est = max(1.0, _safe_float(market.get("daysAgo"), 7.0))
    daily_avg = vol_all / days_est if vol_all else 0
    if daily_avg > 0 and vol_24h > T3_MULT * daily_avg:
        triggers.append(f"T3:vol24h={vol_24h:.0f}")

    if 0 < secs <= T4_SECS:
        triggers.append(f"T4:days={secs/86400:.1f}")

    if not triggers:
        return None

    side               = _decide_side(price)
    amount, tp, sl, label = _size_position(len(triggers))
    score              = min(100, 25 + len(triggers) * 25)

    signal = TradeSignal(
        market_id  = market_id,
        question   = question,
        side       = side,
        token_id   = token_id if side == "YES" else (client.resolve_token_id(market, "NO") or token_id),
        price_now  = price,
        triggers   = triggers,
        score      = score,
        event_date = client.get_event_date(market),
        amount_usd = amount,
        tp_pct     = tp,
        sl_pct     = sl,
        conviction = label,
    )

    logger.info(
        "Signal: %s | %s @ %.2f | conviction=%s amount=$%.2f TP=%.0f%% SL=%.0f%%",
        question[:55], side, price, label, amount, tp * 100, sl * 100,
    )
    return signal
