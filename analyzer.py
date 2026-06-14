"""
Trigger evaluation and trade decision logic.

Pre-filters (all must pass):
  - Price between 0.25 and 0.75 (no extreme underdogs/favorites)
  - Minimum $200 24h volume
  - Must resolve within 7 days (weekly/daily trading)

Need 2+ triggers to fire a trade:
  T1  Price inside 40-60% range (coin-flip uncertainty — market is genuinely unsettled)
  T2  Game is currently in-play (started but not yet resolved — live volatility)
  T3  24h volume > 1.5x daily average (unusual interest)
  T4  Resolves within 48h (imminent resolution — tight time window)
  T5  Bookmaker consensus diverges from Polymarket price by ≥3% (primary edge signal)

Position sizing by triggers fired:
  2 triggers → MED  → $0.35–$0.65  · TP 20% · SL 8%
  3+ triggers→ HIGH → $0.65–$1.00  · TP 25% · SL 10%
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sports_data import get_bookmaker_signal

if TYPE_CHECKING:
    from polymarket_client import PolymarketClient

logger = logging.getLogger(__name__)

# Intra-game period markets resolve in minutes — no edge, catastrophic SL bleed
_PERIOD_RE = re.compile(
    r'\b([1-4][QqHh]|[QqHh][1-4]|1st|2nd|3rd|4th)\b'
    r'|quarter|halftime|half.time|period\s*\d|inning\s*\d',
    re.IGNORECASE,
)

# eventState values that indicate a game is currently live
_LIVE_STATES = {
    "IN_PROGRESS", "INPROGRESS", "IN_PLAY", "INPLAY",
    "LIVE", "STARTED", "ACTIVE_GAME", "ONGOING",
}

MIN_PRICE    = 0.25
MAX_PRICE    = 0.75
MIN_VOL_24H  = 200.0
MAX_DAYS_OUT = 7 * 86_400

T1_LOW  = 0.40
T1_HIGH = 0.60
T3_MULT = 1.5
T4_SECS = 2 * 86_400   # 48h window

_TIERS = {
    2: {"label": "MED",  "min_usd": 0.35, "max_usd": 0.65, "tp": 0.20, "sl": 0.08},
    3: {"label": "HIGH", "min_usd": 0.65, "max_usd": 1.00, "tp": 0.25, "sl": 0.10},
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
    tp_pct:      float     = 0.08
    sl_pct:      float     = 0.12
    conviction:  str       = "MED"


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _parse_ts(raw) -> float:
    if raw is None:
        return 0.0
    try:
        ts = float(raw)
        if ts > 1_000_000_000:
            return ts
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _decide_side(price: float, market: dict | None = None) -> str:
    if market:
        best_ask = _safe_float(market.get("bestAsk") or market.get("bestAskPrice"))
        best_bid = _safe_float(market.get("bestBid") or market.get("bestBidPrice"))
        if best_ask > 0 and best_bid > 0:
            mid = (best_ask + best_bid) / 2
            return "YES" if mid >= 0.50 else "NO"
    return "YES" if price >= 0.50 else "NO"


def _size_position(n_triggers: int) -> tuple[float, float, float, str]:
    tier = _TIERS.get(n_triggers) or _TIERS[3]
    return round(random.uniform(tier["min_usd"], tier["max_usd"]), 2), tier["tp"], tier["sl"], tier["label"]


def _is_live(market: dict, now: float) -> bool:
    """Return True if the game is currently in-play."""
    # Method 1: explicit eventState field
    event_state_raw = market.get("eventState")
    if event_state_raw:
        if isinstance(event_state_raw, dict):
            state_str = str(event_state_raw.get("status") or event_state_raw.get("state") or "").upper()
        else:
            state_str = str(event_state_raw).upper()
        if state_str in _LIVE_STATES:
            return True

    # Method 2: gameStartTime / startTime in the past
    game_start_raw = (
        market.get("gameStartTime")
        or market.get("startTime")
        or market.get("startDate")
    )
    if game_start_raw:
        start_ts = _parse_ts(game_start_raw)
        if 0 < start_ts < now:
            return True

    return False


async def evaluate_market(market: dict, client: "PolymarketClient") -> TradeSignal | None:

    question    = market.get("question") or market.get("title") or ""
    market_id   = market.get("id") or market.get("conditionId") or ""
    market_slug = client.get_market_slug(market)

    # Block intra-game period markets (1Q/2Q/3Q/4Q/1H/2H totals & spreads)
    if _PERIOD_RE.search(question) or _PERIOD_RE.search(market_slug or ""):
        logger.debug("SKIP period-market: %s", question[:60])
        return None

    token_id = client.resolve_token_id(market, "YES")
    if not token_id:
        logger.debug("SKIP no-token: %s", question[:60])
        return None

    secs = client.seconds_to_resolution(market)
    if secs is not None and (secs <= 0 or secs > MAX_DAYS_OUT):
        logger.debug("SKIP time(%.1fd): %s", secs / 86400, question[:60])
        return None

    vol_24h = _safe_float(market.get("volume24hr") or market.get("volume24Hour"))
    if vol_24h > 0 and vol_24h < MIN_VOL_24H:
        logger.debug("SKIP vol(%.0f): %s", vol_24h, question[:60])
        return None

    price = await client.get_market_price(market, token_id)
    if not price:
        logger.debug("SKIP no-price (token=%s…): %s", str(token_id)[:16], question[:60])
        return None

    if price < MIN_PRICE or price > MAX_PRICE:
        logger.debug("SKIP price(%.3f): %s", price, question[:60])
        return None

    triggers: list[str] = []
    now = time.time()

    # T1: price near 50/50 — genuinely contested market
    if T1_LOW <= price <= T1_HIGH:
        triggers.append(f"T1:prob={price:.2f}")

    # T2: game is currently live — detected via eventState or gameStartTime
    if _is_live(market, now):
        triggers.append("T2:live")

    # T3: 24h volume is unusually high relative to historical daily average
    vol_all   = _safe_float(market.get("volume") or market.get("volumeNum"))
    days_est  = max(1.0, _safe_float(market.get("daysAgo"), 7.0))
    daily_avg = vol_all / days_est if vol_all else 0
    if daily_avg > 0 and vol_24h > T3_MULT * daily_avg:
        triggers.append(f"T3:vol24h={vol_24h:.0f}")

    # T4: resolves within 48h — imminent outcome, tighter edge window
    if secs is not None and 0 < secs <= T4_SECS:
        triggers.append(f"T4:hrs={secs/3600:.0f}h")

    # T5: bookmaker consensus diverges from Polymarket by ≥3% — primary edge signal
    bm_side: str | None = None
    bm_result = await get_bookmaker_signal(market, price, client._session)
    if bm_result is not None:
        bm_edge, bm_side = bm_result
        triggers.append(f"T5:bm_edge={bm_edge:.2f}")

    if len(triggers) < 2:
        logger.debug("SKIP triggers=%d (price=%.2f): %s", len(triggers), price, question[:60])
        return None

    # Use bookmaker's side when available — it defines the actual mispricing direction
    side = bm_side if bm_side else _decide_side(price, market)
    trade_token = token_id if side == "YES" else (client.resolve_token_id(market, "NO") or token_id)

    if not await client.has_liquidity(trade_token, min_usd=0.10):
        logger.debug("SKIP no-liquidity: %s", question[:60])
        return None

    amount, tp, sl, label = _size_position(len(triggers))
    score = min(100, 25 + len(triggers) * 25)

    signal = TradeSignal(
        market_id   = market_id,
        market_slug = market_slug,
        question    = question,
        side        = side,
        token_id    = trade_token,
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
        "Signal: %s | %s @ %.2f | slug=%s conviction=%s amount=$%.2f triggers=%s",
        question[:50], side, price, market_slug or "NO-SLUG", label, amount, triggers,
    )
    return signal
