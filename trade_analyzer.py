"""Trade parsing and alert formatting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polymarket_client import MarketTrend


# ------------------------------------------------------------------ #
# Scoring                                                             #
# ------------------------------------------------------------------ #

def score_trade(price: float, size_usd: float) -> tuple[int, str]:
    """Return (score 0-100, recommendation label) for an entry trade."""
    score = 50

    # Price conviction: mid-range prices score higher
    if 0.30 <= price <= 0.70:
        score += 10

    # Size signal
    if size_usd >= 1000:
        score += 20
    elif size_usd >= 500:
        score += 12
    elif size_usd >= 100:
        score += 5

    # High-confidence plays (near certainty)
    if price >= 0.80 or price <= 0.20:
        score += 15

    score = max(0, min(100, score))

    if score >= 75:
        rec = "🟢 STRONG BUY"
    elif score >= 55:
        rec = "🟡 CONSIDER"
    else:
        rec = "🔴 SKIP"

    return score, rec


# ------------------------------------------------------------------ #
# Data classes                                                        #
# ------------------------------------------------------------------ #

@dataclass
class TradeAlert:
    wallet: str
    label: str
    side: str           # BUY or SELL
    outcome: str
    price: float
    size_shares: float
    size_usd: float
    market_name: str
    event_date: str
    score: int
    recommendation: str
    trend: "MarketTrend | None"

    def should_suppress(self) -> bool:
        """Entries below 75 are suppressed. Exits always fire."""
        return self.side == "BUY" and self.score < 75

    def format_telegram(self) -> str:
        if self.side == "BUY":
            return self._fmt_entry()
        return self._fmt_exit()

    def _fmt_entry(self) -> str:
        lines = [
            "━━━━━━  🟢 ENTERED  ━━━━━━",
            "🆕 *New Position Opened*",
            "",
            f"👤 *{self.label}*  (`{self.wallet[:8]}…`)",
            f"📊 {self.market_name}",
            f"📅 Event Date: *{self.event_date}*",
            f"🎯 Outcome: *{self.outcome}*",
            "",
            "📈 *BUY*",
            f"   Price:  {self.price * 100:.1f}¢",
            f"   Size:   {self.size_shares:.1f} shares",
            f"   Value:  ${self.size_usd:,.2f}",
        ]
        lines += _trend_lines(self.trend)
        lines += [
            "",
            f"⚡ Score: {self.score}/100  —  {self.recommendation}",
            "━━━━━━━━━━━━━━━━━━━━━━━",
        ]
        return "\n".join(lines)

    def _fmt_exit(self) -> str:
        lines = [
            "━━━━━━  🔴 EXITED  ━━━━━━",
            "🚪 *Position Closed*",
            "",
            f"👤 *{self.label}*  (`{self.wallet[:8]}…`)",
            f"📊 {self.market_name}",
            f"📅 Event Date: *{self.event_date}*",
            f"🎯 Outcome: *{self.outcome}*",
            "",
            "📉 *SELL*",
            f"   Price:  {self.price * 100:.1f}¢",
            f"   Size:   {self.size_shares:.1f} shares",
            f"   Value:  ${self.size_usd:,.2f}",
        ]
        lines += _trend_lines(self.trend)
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━")
        return "\n".join(lines)


@dataclass
class TraderProfile:
    """Kept for API compatibility."""
    address: str
    total_trades: int = 0
    profit_usd: float = 0.0
    profit_pct: float = 0.0
    win_rate: float = 0.0
    volume_usd: float = 0.0

    def format_telegram(self) -> str:
        return f"📊 Trader `{self.address[:8]}…` — {self.profit_pct:.1f}% profit"


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

def _trend_lines(trend: "MarketTrend | None") -> list[str]:
    if not trend:
        return []
    emoji = {"bullish": "📈", "bearish": "📉", "sideways": "➡️"}.get(trend.direction, "➡️")
    prob = trend.success_probability
    prob_label = "🟢 High" if prob >= 65 else ("🟡 Moderate" if prob >= 45 else "🔴 Low")
    s7 = "+" if trend.change_7d_pct >= 0 else ""
    s1 = "+" if trend.change_1d_pct >= 0 else ""
    return [
        "",
        f"🔮 *Market Trend  —  {emoji} {trend.direction.capitalize()}*",
        f"   7d: {s7}{trend.change_7d_pct:.1f}%   |   24h: {s1}{trend.change_1d_pct:.1f}%",
        f"   Success Probability: *{prob:.0f}%*  ({prob_label})",
    ]


def _extract(trade: dict, *keys: str, default: float = 0.0) -> float:
    for k in keys:
        v = trade.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return default


# ------------------------------------------------------------------ #
# Public API                                                          #
# ------------------------------------------------------------------ #

def parse_trade(
    trade: dict,
    wallet: str,
    label: str,
    market: dict | None,
    event_date: str,
    trend: "MarketTrend | None",
) -> TradeAlert:
    side = (trade.get("side") or trade.get("type") or "BUY").upper()
    if side not in ("BUY", "SELL"):
        side = "BUY"

    outcome = trade.get("outcome") or trade.get("outcomeName") or "?"
    if isinstance(outcome, int):
        outcome = "YES" if outcome == 0 else "NO"

    price = _extract(trade, "price", "outcomePrice", "avgPrice")
    size_shares = _extract(trade, "size", "shares")
    size_usd = _extract(trade, "usdcSize", "amount", "value", "tradeAmount")
    if size_usd == 0.0 and price > 0 and size_shares > 0:
        size_usd = price * size_shares

    market_name = "Unknown Market"
    if market:
        market_name = (
            market.get("question")
            or market.get("title")
            or market.get("name")
            or "Unknown Market"
        )

    score, rec = score_trade(price, size_usd) if side == "BUY" else (0, "")

    return TradeAlert(
        wallet=wallet,
        label=label,
        side=side,
        outcome=str(outcome),
        price=price,
        size_shares=size_shares,
        size_usd=size_usd,
        market_name=market_name,
        event_date=event_date,
        score=score,
        recommendation=rec,
        trend=trend,
    )


def compute_trader_profile(address: str, trades: list[dict]) -> TraderProfile:
    """Kept for API compatibility — not used in this bot."""
    return TraderProfile(address=address, total_trades=len(trades))
