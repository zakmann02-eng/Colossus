"""
Sports data — bookmaker consensus odds via The Odds API.

Set ODDS_API_KEY in Railway env vars (free at the-odds-api.com, 500 req/month).
Uses a 6-hour cache per sport to stay well under the free-tier limit (~200 req/month).

Returns the edge between what bookmakers price a team and what Polymarket prices it.
A positive edge means Polymarket is underpricing the correct side — that's our bet.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
_BASE = "https://api.the-odds-api.com/v4"
MIN_EDGE = 0.07   # minimum 7% gap between bookmaker prob and Polymarket price

# 6-hour cache per sport key — conservative to stay under 500 req/month free tier
_CACHE: dict[str, tuple[float, list]] = {}
_CACHE_TTL = 6 * 3600.0

# Polymarket sport keywords → The Odds API sport key
_SPORT_KEYS = {
    "nba":   "basketball_nba",
    "nfl":   "americanfootball_nfl",
    "mlb":   "baseball_mlb",
    "nhl":   "icehockey_nhl",
    "mls":   "soccer_usa_mls",
    "ncaab": "basketball_ncaab",
    "ncaaf": "americanfootball_ncaaf",
    "wnba":  "basketball_wnba",
}

# Sport keyword aliases for detection
_SPORT_KEYWORDS = {
    "nba":   ["nba", "basketball"],
    "nfl":   ["nfl", "football", "quarterback", "superbowl", "super bowl"],
    "mlb":   ["mlb", "baseball", "pitcher", "world series"],
    "nhl":   ["nhl", "hockey", "stanley cup"],
    "mls":   ["mls", "soccer", "futbol", " fc", "fc ", "united sc", "sporting kc"],
    "ncaab": ["ncaab", "college basketball", "march madness"],
    "ncaaf": ["ncaaf", "college football", "cfp"],
    "wnba":  ["wnba", "women's basketball"],
}


def _detect_sport(market: dict) -> str:
    tags = " ".join(
        str(t).lower() if isinstance(t, str)
        else (t.get("label") or t.get("name") or "").lower()
        for t in (market.get("tags") or [])
    )
    slug     = (market.get("eventSlug") or market.get("slug") or "").lower()
    question = (market.get("question") or "").lower()
    combined = f"{tags} {slug} {question}"

    for sport, keywords in _SPORT_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            return sport
    return ""


def _extract_teams(question: str) -> tuple[str, str]:
    q = question.strip()

    m = re.search(r'^(.+?)\s+v(?:s\.?|ersus)\s+(.+?)(?:\s*[:\-?]|$)', q, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    m = re.search(
        r'will\s+(?:the\s+)?(.+?)\s+(?:beat|defeat|win\s+(?:against|vs\.?))\s+(?:the\s+)?(.+?)(?:\?|$)',
        q, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()

    m = re.search(r'will\s+(?:the\s+)?(.+?)\s+win', q, re.IGNORECASE)
    if m:
        return m.group(1).strip(), ""

    m = re.search(r'^(.+?)\s+to\s+win', q, re.IGNORECASE)
    if m:
        return m.group(1).strip(), ""

    return "", ""


def _token_overlap(a: str, b: str) -> float:
    stop = {"the", "fc", "sc", "city", "united", "athletic"}
    ta = set(re.sub(r'[^\w\s]', '', a).lower().split()) - stop
    tb = set(re.sub(r'[^\w\s]', '', b).lower().split()) - stop
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _find_game(games: list[dict], team_a: str, team_b: str) -> Optional[dict]:
    best_score = 0.4
    best_game  = None
    for g in games:
        home = g.get("home_team", "")
        away = g.get("away_team", "")
        sim_a = max(_token_overlap(team_a, home), _token_overlap(team_a, away))
        if team_b:
            sim_b = max(_token_overlap(team_b, home), _token_overlap(team_b, away))
            score = sim_a + sim_b
            if score > best_score and sim_a > 0.25 and sim_b > 0.25:
                best_score = score
                best_game  = g
        else:
            if sim_a > best_score:
                best_score = sim_a
                best_game  = g
    return best_game


def _consensus_probs(bookmakers: list[dict]) -> dict[str, float]:
    team_probs: dict[str, list[float]] = {}
    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            if mkt.get("key") != "h2h":
                continue
            outcomes = mkt.get("outcomes", [])
            raw: dict[str, float] = {}
            for o in outcomes:
                price = float(o.get("price") or 0)
                if price > 0:
                    raw[o["name"]] = 1.0 / price
            total = sum(raw.values())
            if total <= 0:
                continue
            for name, implied in raw.items():
                team_probs.setdefault(name, []).append(implied / total)
    return {name: sum(ps) / len(ps) for name, ps in team_probs.items() if ps}


async def _fetch_odds(sport_key: str, session: aiohttp.ClientSession) -> list[dict]:
    now = time.time()
    if sport_key in _CACHE:
        ts, data = _CACHE[sport_key]
        if now - ts < _CACHE_TTL:
            logger.debug("Odds cache hit for %s (age %.0fmin)", sport_key, (now - ts) / 60)
            return data

    url = f"{_BASE}/sports/{sport_key}/odds"
    params = {
        "apiKey":      ODDS_API_KEY,
        "regions":     "us",
        "markets":     "h2h",
        "oddsFormat":  "decimal",
        "dateFormat":  "iso",
    }
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                games = await r.json()
                _CACHE[sport_key] = (now, games)
                remaining = r.headers.get("x-requests-remaining", "?")
                logger.info("Odds API: %d games for %s (quota remaining: %s)", len(games), sport_key, remaining)
                return games
            elif r.status == 401:
                logger.error("Odds API: invalid ODDS_API_KEY")
            elif r.status == 422:
                logger.debug("Odds API: sport %s not currently available", sport_key)
            elif r.status == 429:
                logger.warning("Odds API: monthly quota exhausted")
            else:
                logger.debug("Odds API status %d for %s", r.status, sport_key)
    except Exception as exc:
        logger.debug("Odds API fetch error: %s", exc)

    _CACHE[sport_key] = (now, [])
    return []


async def get_bookmaker_signal(
    market: dict,
    poly_price: float,
    session: aiohttp.ClientSession,
) -> Optional[tuple[float, str]]:
    if not ODDS_API_KEY:
        return None

    sport = _detect_sport(market)
    sport_key = _SPORT_KEYS.get(sport)
    if not sport_key:
        logger.debug("Sport not detected for: %s", (market.get("question") or "")[:60])
        return None

    question = market.get("question") or ""
    team_a, team_b = _extract_teams(question)
    if not team_a:
        logger.debug("Could not extract teams from: %s", question[:60])
        return None

    games = await _fetch_odds(sport_key, session)
    if not games:
        return None

    game = _find_game(games, team_a, team_b)
    if not game:
        logger.debug("No bookmaker game match: '%s' / '%s' in %s", team_a, team_b, sport_key)
        return None

    probs = _consensus_probs(game.get("bookmakers", []))
    if not probs:
        return None

    home = game.get("home_team", "")
    away = game.get("away_team", "")
    sim_home = _token_overlap(team_a, home)
    sim_away = _token_overlap(team_a, away)
    focus_team = home if sim_home >= sim_away else away

    bm_prob: Optional[float] = None
    for name, prob in probs.items():
        if _token_overlap(team_a, name) >= 0.5:
            bm_prob = prob
            break
    if bm_prob is None:
        bm_prob = probs.get(focus_team)
    if bm_prob is None:
        logger.debug("Could not map '%s' to bookmaker team in %s", team_a, probs)
        return None

    raw_edge = bm_prob - poly_price
    edge     = abs(raw_edge)
    side     = "YES" if raw_edge > 0 else "NO"

    logger.info(
        "Bookmaker signal | '%s' | bm=%.3f poly=%.3f edge=+%.3f → %s",
        question[:55], bm_prob, poly_price, edge, side,
    )

    if edge < MIN_EDGE:
        logger.debug("Edge %.3f below threshold %.3f — skip", edge, MIN_EDGE)
        return None

    return edge, side
