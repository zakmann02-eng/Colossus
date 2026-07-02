"""
Performance tracker — learns from closed trades to improve future sizing.

Reads trade_log.csv after each close and computes per-trigger win rates.
Returns a size multiplier (0.5x–1.5x) that scales position size up when a
trigger combination has a strong track record and down when it has a weak one.

Minimum sample guard: no adjustment fires until MIN_SAMPLE trades exist for
that trigger key. Below that threshold the multiplier is always 1.0 (neutral).
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from collections import defaultdict

logger = logging.getLogger(__name__)

_TRADE_LOG   = Path("trade_log.csv")
MIN_SAMPLE   = 5    # minimum trades before a trigger key influences sizing
MAX_MULT     = 1.5  # cap upward adjustment
MIN_MULT     = 0.5  # cap downward adjustment


def _load_trades() -> list[dict]:
    if not _TRADE_LOG.exists():
        return []
    try:
        with _TRADE_LOG.open(newline="") as f:
            return list(csv.DictReader(f))
    except Exception as exc:
        logger.warning("performance_tracker: could not read trade_log.csv: %s", exc)
        return []


def _parse_pnl(pnl_str: str) -> float:
    try:
        return float(pnl_str.strip().replace("%", "")) / 100
    except (ValueError, AttributeError):
        return 0.0


def _trigger_keys(triggers_str: str) -> list[str]:
    if not triggers_str:
        return []
    parts = [t.strip().split(":")[0] for t in triggers_str.split(",") if t.strip()]
    parts = sorted(set(parts))
    keys = list(parts)
    if len(parts) >= 2:
        keys.append("+".join(parts))
    return keys


def compute_stats() -> dict[str, dict]:
    trades = _load_trades()
    if not trades:
        return {}

    buckets: dict[str, list[float]] = defaultdict(list)

    for row in trades:
        pnl = _parse_pnl(row.get("pnl_pct", "0"))
        for key in _trigger_keys(row.get("triggers", "")):
            buckets[key].append(pnl)

    stats: dict[str, dict] = {}
    for key, pnls in buckets.items():
        wins = sum(1 for p in pnls if p > 0)
        stats[key] = {
            "count":    len(pnls),
            "wins":     wins,
            "win_rate": wins / len(pnls),
            "avg_pnl":  sum(pnls) / len(pnls),
        }
    return stats


def get_size_multiplier(triggers: list[str]) -> float:
    if not triggers:
        return 1.0

    stats = compute_stats()
    if not stats:
        return 1.0

    labels = sorted(set(t.split(":")[0] for t in triggers))
    combo  = "+".join(labels)
    candidates = ([combo] if len(labels) >= 2 else []) + labels

    for key in candidates:
        s = stats.get(key)
        if s and s["count"] >= MIN_SAMPLE:
            wr = s["win_rate"]
            if wr >= 0.70:
                mult = MAX_MULT
            elif wr >= 0.55:
                mult = 1.2
            elif wr >= 0.45:
                mult = 1.0
            elif wr >= 0.30:
                mult = 0.8
            else:
                mult = MIN_MULT
            logger.info(
                "Learned sizing: key=%s win_rate=%.0f%% (n=%d) → %.1fx multiplier",
                key, wr * 100, s["count"], mult,
            )
            return mult

    return 1.0


def get_performance_summary() -> str:
    trades = _load_trades()
    if not trades:
        return "📈 No closed trades yet — nothing to learn from."

    stats = compute_stats()
    total = len(trades)
    total_wins = sum(1 for t in trades if _parse_pnl(t.get("pnl_pct", "0")) > 0)
    overall_pnl = sum(_parse_pnl(t.get("pnl_pct", "0")) for t in trades) / total

    lines = [
        f"📈 Performance Summary — {total} closed trade(s)",
        f"Overall: {total_wins}/{total} wins ({total_wins/total:.0%}) | avg P&L {overall_pnl:+.1%}\n",
        "Trigger breakdown (≥{} trades):".format(MIN_SAMPLE),
    ]

    shown = sorted(
        [(k, s) for k, s in stats.items() if s["count"] >= MIN_SAMPLE],
        key=lambda x: x[1]["win_rate"],
        reverse=True,
    )

    if not shown:
        lines.append(f"  (need {MIN_SAMPLE}+ trades per trigger before stats are reliable)")
    else:
        for key, s in shown:
            bar = "🟢" if s["win_rate"] >= 0.55 else ("🟡" if s["win_rate"] >= 0.40 else "🔴")
            lines.append(
                f"  {bar} {key:<10} {s['wins']}/{s['count']} wins "
                f"({s['win_rate']:.0%}) avg {s['avg_pnl']:+.1%}"
            )

    conv_buckets: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        conv = t.get("conviction", "").strip()
        if conv:
            conv_buckets[conv].append(_parse_pnl(t.get("pnl_pct", "0")))

    if conv_buckets:
        lines.append("\nConviction breakdown:")
        for conv in ["HIGH", "MED", "LOW", "SYNCED"]:
            pnls = conv_buckets.get(conv)
            if not pnls:
                continue
            wins = sum(1 for p in pnls if p > 0)
            lines.append(
                f"  {conv:<8} {wins}/{len(pnls)} wins ({wins/len(pnls):.0%}) "
                f"avg {sum(pnls)/len(pnls):+.1%}"
            )

    return "\n".join(lines)
