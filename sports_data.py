"""
Bookmaker odds integration via The Odds API.

Fetches consensus win-probability for the team/player relevant to a Polymarket
market question and compares it to the current Polymarket price.

Returns (edge, side) when bookmaker consensus diverges by ≥ MIN_EDGE from the
Polymarket price, else None.

Cache: per-sport event list is cached for _CACHE_TTL seconds to stay well
within the free-tier rate limit (500 requests/month).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

MIN_EDGE   = 0.03          # 3 % minimum edge to fire T5
_CACHE_TTL = 12 * 3600.0   # 12 hours per sport key

# ── Odds API sport keys ────────────────────────────────────────────────────────
_SPORT_KEYS: dict[str, str] = {
    "soccer":       "soccer_epl",
    "football":     "americanfootball_nfl",
    "basketball":   "basketball_nba",
    "baseball":     "baseball_mlb",
    "hockey":       "icehockey_nhl",
    "mma":          "mma_mixed_martial_arts",
    "ufc":          "mma_mixed_martial_arts",
    "boxing":       "boxing",
    "tennis":       "tennis_atp_french_open",
    "cricket":      "cricket_test_match",
    "golf":         "golf_pga_championship",
    "rugby":        "rugbyleague_nrl",
    "aussierules":  "aussierules_afl",
    "worldcup":     "soccer_fifa_world_cup",
    "copa":         "soccer_conmebol_copa_libertadores",
    "champions":    "soccer_uefa_champs_league",
    "europa":       "soccer_uefa_europa_league",
    "bundesliga":   "soccer_germany_bundesliga",
    "laliga":       "soccer_spain_la_liga",
    "seriea":       "soccer_italy_serie_a",
    "ligue1":       "soccer_france_ligue_one",
    "nba":          "basketball_nba",
    "wnba":         "basketball_wnba",
    "nfl":          "americanfootball_nfl",
    "ncaaf":        "americanfootball_ncaaf",
    "mlb":          "baseball_mlb",
    "nhl":          "icehockey_nhl",
    "f1":           "motorsport_formula_one_winner",
}

# ── Keywords → sport key lookup ────────────────────────────────────────────────
_SPORT_KEYWORDS: dict[str, list[str]] = {
    "worldcup":   ["world cup", "fifa world cup", "copa mundial", "worldcup",
                   "world cup 2026", "2026 world cup"],
    "ufc":        ["ufc", "ultimate fighting championship"],
    "mma":        ["mma", "bellator", "one championship", "pfl"],
    "boxing":     ["boxing", "wbc", "wba", "ibf", "wbo", "heavyweight fight",
                   "lightweight fight", "welterweight"],
    "tennis":     ["wta", "atp", "tennis", "wimbledon", "french open",
                   "us open", "australian open", "roland garros"],
    "soccer":     ["premier league", "epl", "la liga", "serie a", "bundesliga",
                   "ligue 1", "mls", "soccer", "football match",
                   "champions league", "europa league"],
    "champions":  ["champions league", "ucl"],
    "europa":     ["europa league", "uel"],
    "basketball": ["nba", "wnba", "basketball"],
    "baseball":   ["mlb", "baseball"],
    "hockey":     ["nhl", "hockey", "ice hockey"],
    "football":   ["nfl", "ncaaf", "super bowl", "american football"],
    "golf":       ["pga", "golf", "masters", "open championship"],
    "f1":         ["formula 1", "formula one", "f1", "grand prix"],
    "copa":       ["copa libertadores", "copa sudamericana"],
}


# ── Cache ──────────────────────────────────────────────────────────────────────

_cache: dict[str, tuple[float, list[dict]]] = {}
_cache_lock = asyncio.Lock()


def _detect_sport(text: str) -> str | None:
    t = text.lower()
    for sport, keywords in _SPORT_KEYWORDS.items():
        for kw in keywords:
            if kw in t:
                return sport
    return None


def _american_to_prob(odds: int) -> float:
    if odds > 0:
        return 100 / (odds + 100)
    return abs(odds) / (abs(odds) + 100)


def _extract_consensus_prob(outcomes: list[dict], target: str) -> float | None:
    target_l = target.lower()
    probs: list[float] = []
    for outcome in outcomes:
        name = (outcome.get("name") or "").lower()
        if target_l not in name and name not in target_l:
            continue
        price = outcome.get("price")
        if price is None:
            continue
        try:
            p = float(price)
            # Decimal odds (>= 1.0) vs American odds
            if p >= 1.0:
                probs.append(1.0 / p)
            else:
                probs.append(p)
        except (TypeError, ValueError):
            pass
    return sum(probs) / len(probs) if probs else None


async def _fetch_sport_events(sport_key: str, session: aiohttp.ClientSession) -> list[dict]:
    """Fetch upcoming events for a sport key with caching."""
    api_key = os.getenv("ODDS_API_KEY", "").strip()
    if not api_key:
        return []

    async with _cache_lock:
        cached = _cache.get(sport_key)
        if cached and time.time() - cached[0] < _CACHE_TTL:
            return cached[1]

    url = (
        f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
        f"?apiKey={api_key}&regions=us&markets=h2h&oddsFormat=decimal&dateFormat=iso"
    )
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                data = await resp.json()
                async with _cache_lock:
                    _cache[sport_key] = (time.time(), data)
                logger.debug("Odds API: fetched %d events for %s", len(data), sport_key)
                return data
            elif resp.status == 401:
                logger.warning("Odds API: invalid key (401)")
            elif resp.status == 422:
                logger.debug("Odds API: sport %s not available (422)", sport_key)
            elif resp.status == 429:
                logger.warning("Odds API: rate limit hit (429)")
            else:
                logger.debug("Odds API: HTTP %d for %s", resp.status, sport_key)
    except asyncio.TimeoutError:
        logger.debug("Odds API: timeout for %s", sport_key)
    except Exception as exc:
        logger.debug("Odds API error for %s: %s", sport_key, exc)
    return []


def _find_matching_event(events: list[dict], question: str) -> dict | None:
    q = question.lower()
    best: dict | None = None
    best_score = 0

    for event in events:
        home = (event.get("home_team") or "").lower()
        away = (event.get("away_team") or "").lower()
        score = 0
        if home and home in q:
            score += 2
        if away and away in q:
            score += 2
        for part in home.split():
            if len(part) > 3 and part in q:
                score += 1
        for part in away.split():
            if len(part) > 3 and part in q:
                score += 1
        if score > best_score:
            best_score = score
            best = event

    return best if best_score >= 2 else None


async def get_bookmaker_signal(
    market: dict,
    polymarket_price: float,
    session: aiohttp.ClientSession,
) -> tuple[float, str] | None:
    """
    Compare bookmaker consensus to Polymarket price.

    Returns (edge, side) if bookmaker consensus diverges by ≥ MIN_EDGE,
    where side is "YES" if bookmaker says the outcome is more likely than
    Polymarket implies, else "NO".

    Returns None if no signal or no data available.
    """
    question = market.get("question") or market.get("title") or ""
    if not question:
        return None

    sport = _detect_sport(question)
    if not sport:
        return None

    sport_key = _SPORT_KEYS.get(sport)
    if not sport_key:
        return None

    events = await _fetch_sport_events(sport_key, session)
    if not events:
        return None

    event = _find_matching_event(events, question)
    if not event:
        logger.debug("Odds API: no event match for: %s", question[:60])
        return None

    home = event.get("home_team") or ""
    away = event.get("away_team") or ""
    q_lower = question.lower()

    target_team: str | None = None
    if home.lower() in q_lower or any(p in q_lower for p in home.lower().split() if len(p) > 3):
        target_team = home
    elif away.lower() in q_lower or any(p in q_lower for p in away.lower().split() if len(p) > 3):
        target_team = away

    if not target_team:
        return None

    all_outcomes: list[dict] = []
    for bm in event.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt.get("key") == "h2h":
                all_outcomes.extend(mkt.get("outcomes", []))

    bm_prob = _extract_consensus_prob(all_outcomes, target_team)
    if bm_prob is None:
        logger.debug("Odds API: no odds for %s in event %s", target_team, event.get("id"))
        return None

    all_team_probs: list[float] = []
    for outcome in all_outcomes:
        price = outcome.get("price")
        if price is None:
            continue
        try:
            p = float(price)
            all_team_probs.append(1.0 / p if p >= 1.0 else p)
        except (TypeError, ValueError):
            pass

    if all_team_probs:
        n_bm = max(1, len(event.get("bookmakers", [])))
        total_raw = sum(all_team_probs) / (len(all_team_probs) / n_bm)
        if total_raw > 0:
            bm_prob = bm_prob / total_raw

    bm_prob = max(0.01, min(0.99, bm_prob))
    edge = bm_prob - polymarket_price

    if abs(edge) < MIN_EDGE:
        logger.debug(
            "Odds API: edge %.2f%% below threshold for %s",
            abs(edge) * 100, question[:50],
        )
        return None

    side = "YES" if edge > 0 else "NO"
    logger.info(
        "T5 signal: %s | bm_prob=%.3f poly_price=%.3f edge=%.2f%% side=%s",
        question[:50], bm_prob, polymarket_price, abs(edge) * 100, side,
    )
    return abs(edge), side
