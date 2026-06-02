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

Position sizing by conviction (triggers fired):
  2 triggers → low  → $0.10–$0.50
  3 triggers → med  → $0.50–$1.25
  4 triggers → high → $1.25–$2.00

TP/SL scaled by conviction:
  2 triggers → TP 8%  / SL 8%
  3 triggers → TP 12% / SL 10%
  4 triggers → TP 15% / SL 10%
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polymarket_client import PolymarketClient

logger = logging.getLogger(__name__)

# ── Pre-filters ───────────────────────────────────────────────────────────────
MIN_PRICE    = 0.05
MAX_PRICE    = 0.95
MIN_VOL_24H  = 100.0
MAX_DAYS_OUT = 30 * 86_400

# ── Trigger thresholds ────────────────────────────────────────────────────────
T1_LOW  = 0.40  
T1_HIGH = 0.60   
T2_MOVE = 0.03
T3_MULT = 1.5
T4_SECS = 14 * 86_400

# ── Conviction tiers (keyed by number of triggers fired) ─────────────────────
_TIERS = {
    2: {"label": "LOW",  "min_usd": 0.10, "max_usd": 0.50, "tp": 0.08, "sl": 0.08},
    3: {"label": "MED",  "min_usd": 0.50, "max_usd": 1.25, "tp": 0.12, "sl": 0.10},
    4: {"label": "HIGH", "min_usd": 1.25, "max_usd": 2.00, "tp": 0.15, "sl": 0.10},
}


@dataclass
class TradeSignal:
    market_id:   str
    question:    str
    side:        str
    token_id:    str
    price_now:   float
    triggers:    list[str] = field(default_factory=list)
    score:       int       = 0
    event_date:  str       = "N/A"
    amount_usd:  float     = 0.50   # sized by conviction
    tp_pct:      float     = 0.10   # take profit threshold
    sl_pct:      float     = 0.10   # stop loss threshold
    conviction:  str       = "LOW"


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _decide_side(price: float) -> str:
    return "NO" if price > T1_HIGH else "YES"


def _size_position(n_triggers: int) -> tuple[float, float, float, str]:
    """Return (amount_usd, tp_pct, sl_pct, label) based on triggers fired."""
    tier = _TIERS.get(n_triggers) or _TIERS[4 if n_triggers > 4 else 2]
    amount = round(random.uniform(tier["min_usd"], tier["max_usd"]), 2)
    return amount, tier["tp"], tier["sl"], tier["label"]


async def evaluate_market(
    market: dict,
    client: "PolymarketClient",
) -> TradeSignal | None:
    """Run pre-filters then triggers. Return sized signal if >=2 fire."""

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

    # ── T4: event active or imminent ──────────────────────────────────────
    if 0 < secs <= T4_SECS:
        triggers.append(f"T4:days={secs/86400:.1f}")

    if len(triggers) < 2:
        return None

    side                    = _decide_side(price)
    amount, tp, sl, label   = _size_position(len(triggers))
    score                   = min(100, 50 + len(triggers) * 15)

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
