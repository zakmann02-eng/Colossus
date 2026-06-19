"""
Trigger evaluation and trade decision logic.

Pre-filters (all must pass):
  - Price between 0.05 and 0.95
  - Minimum $75 24h volume
  - Resolution time must be known and within 7 days

Requires 2+ triggers to fire a trade:
  T1  Price outside 35-65% range
  T2  Price moved >= 1% in last 15 min (significant momentum)
  T3  24h volume > 2x daily average (unusual activity)
  T5  Bookmaker consensus >= 3% edge over Polymarket price

  Note: T4 (resolves within 7 days) is a pre-filter only — it no longer
  counts toward the trigger threshold because it fires on every eligible
  market, adding no discriminating signal value.

Position sizing by triggers fired:
  2 triggers → LOW  → $0.10–$0.35 · TP 15% · SL 6%
  3 triggers → MED  → $0.35–$0.65 · TP 20% · SL 8%
  4+ triggers→ HIGH → $0.65–$1.00 · TP 25% · SL 10%
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sports_data import get_bookmaker_signal

if TYPE_CHECKING:
    from polymarket_client import PolymarketClient

logger = logging.getLogger(__name__)

MIN_PRICE    = 0.05
MAX_PRICE    = 0.95
MIN_VOL_24H  = 75.0
MAX_DAYS_OUT = 7 * 86_400
MIN_TRIGGERS = 2

_skip_log_count = 0  # log first N skips at INFO so Railway shows why

T1_LOW  = 0.35
T1_HIGH = 0.65
T2_MOVE = 0.01
T3_MULT = 2.0
T4_SECS = 7 * 86_400
T5_MIN_EDGE = 0.03

_TIERS = {
    2: {"label": "LOW",  "min_usd": 0.10, "max_usd": 0.35, "tp": 0.15, "sl": 0.06},
    3: {"label": "MED",  "min_usd": 0.35, "max_usd": 0.65, "tp": 0.20, "sl": 0.08},
    4: {"label": "HIGH", "min_usd": 0.65, "max_usd": 1.00, "tp": 0.25, "sl": 0.10},
}


@dataclass
class TradeSignal:
    market_id:   str
    market_slug: str
    question:    str
    side:        str
    token_id:    str
    price_now:   float
    triggers:    list[str] = field(default_factory=list)
    score:       int       = 0
    event_date:  str       = "N/A"
    amount_usd:  float     = 0.50
    tp_pct:      float     = 0.20
    sl_pct:      float     = 0.08
    conviction:  str       = "MED"


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _decide_side(price: float, market: dict | None = None) -> str:
    return "NO" if price > 0.50 else "YES"


def _size_position(n_triggers: int) -> tuple[float, float, float, str]:
    tier = _TIERS.get(n_triggers) or _TIERS[max(_TIERS)]
    return round(random.uniform(tier["min_usd"], tier["max_usd"]), 2), tier["tp"], tier["sl"], tier["label"]


async def evaluate_market(market: dict, client: "PolymarketClient") -> TradeSignal | None:
    global _skip_log_count

    question    = market.get("question") or market.get("title") or ""
    market_id   = market.get("id") or market.get("conditionId") or ""
    market_slug = client.get_market_slug(market)

    token_id = client.resolve_token_id(market, "YES")
    if not token_id:
        logger.debug("SKIP no-token: %s", question[:60])
        return None

    secs = client.seconds_to_resolution(market)
    if secs is None:
        if _skip_log_count < 30:
            _skip_log_count += 1
            logger.info("SKIP no-date [diag]: slug=%s q=%s", market_slug[:30], question[:50])
        else:
            logger.debug("SKIP no-date: %s", question[:60])
        return None
    if secs <= 0 or secs > MAX_DAYS_OUT:
        if _skip_log_count < 30:
            _skip_log_count += 1
            logger.info("SKIP time(%.1fd) [diag]: slug=%s q=%s", secs / 86400, market_slug[:30], question[:50])
        else:
            logger.debug("SKIP time(%.1fd): %s", secs / 86400, question[:60])
        return None

    vol_24h = _safe_float(market.get("volume24hr") or market.get("volume24Hour"))
    if vol_24h < MIN_VOL_24H:
        logger.debug("SKIP vol(%.0f): %s", vol_24h, question[:60])
        return None

    price = await client.get_market_price(market, token_id)
    if not price:
        if _skip_log_count < 30:
            _skip_log_count += 1
            logger.info("SKIP no-price [diag]: slug=%s token=%s q=%s", market_slug[:30], str(token_id)[:20], question[:50])
        else:
            logger.debug("SKIP no-price: %s", question[:60])
        return None

    if price < MIN_PRICE or price > MAX_PRICE:
        logger.debug("SKIP price(%.3f): %s", price, question[:60])
        return None

    triggers: list[str] = []

    if price < T1_LOW or price > T1_HIGH:
        triggers.append(f"T1:prob={price:.2f}")

    price_15m = await client.get_price_15min_ago(market, token_id)
    if price_15m and price_15m > 0:
        move = abs(price - price_15m) / price_15m
        if move >= T2_MOVE:
            triggers.append(f"T2:move={move:.1%}")

    vol_all   = _safe_float(market.get("volume") or market.get("volumeNum"))
    days_est  = max(1.0, _safe_float(market.get("daysAgo"), 7.0))
    daily_avg = vol_all / days_est if vol_all else 0
    if daily_avg > 0 and vol_24h > T3_MULT * daily_avg:
        triggers.append(f"T3:vol24h={vol_24h:.0f}")

    # T5: bookmaker consensus diverges from Polymarket by >= 3% — primary edge signal
    bm_side: str | None = None
    try:
        bm_result = await get_bookmaker_signal(market, price, client._session)
        if bm_result is not None:
            bm_edge, bm_side = bm_result
            triggers.append(f"T5:bm_edge={bm_edge:.2f}")
    except Exception as exc:
        logger.debug("T5 error: %s", exc)

    if len(triggers) < MIN_TRIGGERS:
        logger.debug("SKIP triggers=%d (price=%.2f): %s", len(triggers), price, question[:60])
        return None

    # Must have at least one directional signal — T1 (price bias) or T5 (bookmaker edge).
    # T2/T3/T4 alone only confirm activity, not which side to bet.
    has_directional = any(t.startswith("T1:") or t.startswith("T5:") for t in triggers)
    if not has_directional:
        logger.debug("SKIP no-directional (T1/T5 absent): %s", question[:60])
        return None

    # Bookmaker's side defines the actual mispricing direction when available
    side               = bm_side if bm_side else _decide_side(price, market)
    amount, tp, sl, label = _size_position(len(triggers))
    score              = min(100, len(triggers) * 25)

    signal = TradeSignal(
        market_id   = market_id,
        market_slug = market_slug,
        question    = question,
        side        = side,
        token_id    = token_id if side == "YES" else (client.resolve_token_id(market, "NO") or token_id),
        price_now   = price,
        triggers    = triggers,
        score       = score,
        event_date  = client.get_event_date(market),
        amount_usd  = amount,
        tp_pct      = tp,
        sl_pct      = sl,
        conviction  = label,
    )

    logger.info(
        "Signal: %s | %s @ %.2f | slug=%s conviction=%s amount=$%.2f",
        question[:50], side, price, market_slug or "NO-SLUG", label, amount,
    )
    return signal
