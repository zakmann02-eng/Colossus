"""
Trigger evaluation and trade decision logic.

Pre-filters (all must pass):
  - Price between 0.05 and 0.95
  - Minimum $75 24h volume
  - Resolution time must be known and within 60 days

Requires 2+ effective triggers to fire a trade:
  T1  Price outside 45-55% range (price bias signal)
  T2  Price moved >= 0.5% in last 15 min (momentum)
  T3  24h volume > 2x daily average (unusual activity)
  T5  Bookmaker consensus >= 3% edge over Polymarket price (bonus signal)

  T5 is a high-value bonus: when it fires it counts double (as 2 triggers),
  defines trade direction, and pushes to HIGH conviction.
  T1/T2/T3 can fire a trade independently with any 2 of them.

Position sizing by effective triggers fired:
  2 triggers → LOW  → $0.20–$0.35 · TP 15% · SL 6%
  3 triggers → MED  → $0.35–$0.65 · TP 20% · SL 8%
  4+ triggers→ HIGH → $0.65–$1.00 · TP 25% · SL 10%
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sports_data import get_bookmaker_signal
from performance_tracker import get_size_multiplier
import scan_log

if TYPE_CHECKING:
    from polymarket_client import PolymarketClient

logger = logging.getLogger(__name__)

MIN_PRICE    = 0.05
MAX_PRICE    = 0.95
MIN_VOL_24H  = 0.0   # API does not return volume24hr — gate disabled
MAX_DAYS_OUT = 60 * 86_400  # extended to 60 days to catch NFL preseason + early regular season
MIN_TRIGGERS = 2  # any 2 of T1/T2/T3/T5; T5 counts double when present

_skip_log_count = 0  # log first N skips at INFO so Railway shows why
_date_diag_done = False  # log date fields of first market once per session

# Per-scan skip counters — reset by bot.py before each scan
_skip_counts: dict[str, int] = {}

def reset_skip_counts() -> None:
    global _skip_counts
    _skip_counts = {}

def _skip(reason: str) -> None:
    _skip_counts[reason] = _skip_counts.get(reason, 0) + 1

def get_skip_summary() -> str:
    if not _skip_counts:
        return "no skips"
    return " | ".join(f"{k}={v}" for k, v in sorted(_skip_counts.items(), key=lambda x: -x[1]))

T1_LOW  = 0.45
T1_HIGH = 0.55
T2_MOVE = 0.005  # 0.5% momentum threshold
T3_MULT = 2.0
T5_MIN_EDGE = 0.03

_TIERS = {
    2: {"label": "LOW",  "min_usd": 0.20, "max_usd": 0.35, "tp": 0.15, "sl": 0.06},
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
    global _skip_log_count, _date_diag_done

    question    = market.get("question") or market.get("title") or ""
    market_id   = market.get("id") or market.get("conditionId") or ""
    market_slug = client.get_market_slug(market)

    token_id = client.resolve_token_id(market, "YES")
    if not token_id:
        _skip("no-token")
        logger.debug("SKIP no-token: %s", question[:60])
        return None

    if not _date_diag_done:
        _date_diag_done = True
        logger.info(
            "DATE-DIAG first market: resolutionTime=%s endDate=%s startTime=%s gameStartTime=%s q=%s",
            market.get("resolutionTime"), market.get("endDate"),
            market.get("startTime"), market.get("gameStartTime"),
            question[:50],
        )

    secs = client.seconds_to_resolution(market)
    if secs is None:
        _skip("no-date")
        if _skip_log_count < 10:
            _skip_log_count += 1
            logger.info("SKIP no-date [diag]: slug=%s q=%s", market_slug[:30], question[:50])
        else:
            logger.debug("SKIP no-date: %s", question[:60])
        return None

    # Past games and far-future markets go to debug — they're expected and burn INFO quota
    if secs <= 0:
        _skip("past")
        logger.debug("SKIP past(%.1fd): %s", secs / 86400, question[:60])
        return None
    if secs > MAX_DAYS_OUT:
        _skip("far-future")
        logger.debug("SKIP far-future(%.1fd): %s", secs / 86400, question[:60])
        return None

    # Log every upcoming market at INFO so we know what's in the trading window
    logger.info("UPCOMING(%.1fd): slug=%s q=%s", secs / 86400, market_slug[:35], question[:60])

    price = await client.get_market_price(market, token_id)
    if not price:
        _skip("no-price")
        if _skip_log_count < 10:
            _skip_log_count += 1
            logger.info("SKIP no-price [diag]: slug=%s token=%s q=%s",
                        market_slug[:30], str(token_id)[:20], question[:50])
        else:
            logger.debug("SKIP no-price: %s", question[:60])
        return None

    if price < MIN_PRICE or price > MAX_PRICE:
        _skip("price-extreme")
        logger.debug("SKIP price(%.3f): %s", price, question[:60])
        return None

    # CLOB last-trade price check — log but do NOT hard-block.
    # TP/SL falls back to portfolio price when CLOB is unavailable.
    clob_price = await client.get_current_price(token_id)
    if clob_price is None:
        if _skip_log_count < 10:
            _skip_log_count += 1
            logger.info("WARN illiquid (no CLOB last-trade) [diag]: slug=%s q=%s",
                        market_slug[:30], question[:50])
        else:
            logger.debug("WARN illiquid (no CLOB last-trade): %s", question[:60])
        scan_log.add(market_slug, question, price, "WARN", "no CLOB last-trade — continuing", [])
        # Continue evaluation — position_manager has portfolio price fallback for TP/SL

    triggers: list[str] = []

    if price < T1_LOW or price > T1_HIGH:
        triggers.append(f"T1:prob={price:.2f}")

    price_15m = await client.get_price_15min_ago(market, token_id)
    if price_15m and price_15m > 0:
        move = abs(price - price_15m) / price_15m
        if move >= T2_MOVE:
            triggers.append(f"T2:move={move:.1%}")

    vol_all   = _safe_float(market.get("volume") or market.get("volumeNum"))
    days_est  = max(1.0, _safe_float(market.get("daysAgo"), 30.0))
    daily_avg = vol_all / days_est if vol_all else 0
    if daily_avg > 0 and vol_24h > T3_MULT * daily_avg:
        triggers.append(f"T3:vol24h={vol_24h:.0f}")

    # T5: bookmaker consensus diverges from Polymarket by >= 3%
    bm_side: str | None = None
    try:
        bm_result = await get_bookmaker_signal(market, price, client._session)
        if bm_result is not None:
            bm_edge, bm_side = bm_result
            triggers.append(f"T5:bm_edge={bm_edge:.2f}")
    except Exception as exc:
        logger.debug("T5 error: %s", exc)

    # T5 counts as 2 triggers — bookmaker edge is a confirmed external signal.
    # A lone T5 (effective_count=2) is sufficient to trade.
    has_t5 = any(t.startswith("T5:") for t in triggers)
    effective_count = len(triggers) + (1 if has_t5 else 0)

    if effective_count < MIN_TRIGGERS:
        logger.debug("SKIP triggers=%d effective=%d (price=%.2f): %s",
                     len(triggers), effective_count, price, question[:60])
        scan_log.add(market_slug, question, price, "SKIP",
                     f"only {len(triggers)} trigger(s) fired (effective={effective_count}, need {MIN_TRIGGERS})",
                     triggers)
        return None

    # T5 defines trade direction when available; fall back to price bias.
    side                  = bm_side if bm_side else _decide_side(price, market)
    amount, tp, sl, label = _size_position(effective_count)
    score                 = min(100, effective_count * 25)

    # Apply learned multiplier from historical trigger performance.
    # Neutral (1.0x) until MIN_SAMPLE trades exist for this trigger combo.
    learned_mult = get_size_multiplier(triggers)
    if learned_mult != 1.0:
        import os
        raw_amount = amount
        lo = float(os.getenv("MIN_TRADE_USD", "0.10"))
        hi = float(os.getenv("MAX_TRADE_USD", "1.00"))
        amount = round(max(lo, min(hi, amount * learned_mult)), 2)
        logger.info("Learned sizing applied: %.2f → %.2f (%.1fx)", raw_amount, amount, learned_mult)

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

    scan_log.add(market_slug, question, price, "SIGNAL",
                 f"{side} {label} ${amount:.2f} TP={tp:.0%} SL={sl:.0%}", triggers, label)
    logger.info(
        "Signal: %s | %s @ %.2f | slug=%s conviction=%s amount=$%.2f",
        question[:50], side, price, market_slug or "NO-SLUG", label, amount,
    )
    return signal
