"""
Sports data — bookmaker consensus odds via The Odds API.

Set ODDS_API_KEY in Railway env vars (free at the-odds-api.com, 500 req/month).
Uses a 12-hour cache per sport. With ~15 active sport keys that's ~30 req/day (~300/month).

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
MIN_EDGE = 0.05   # minimum 5% gap between bookmaker prob and Polymarket price

# 12-hour cache per sport key — with more sports, keeps us under 500 req/month free tier
_CACHE: dict[str, tuple[float, list]] = {}
_CACHE_TTL = 12 * 3600.0

# Polymarket sport keywords → The Odds API sport key
_SPORT_KEYS = {
    # US major leagues
    "nba":          "basketball_nba",
    "nfl":          "americanfootball_nfl",
    "mlb":          "baseball_mlb",
    "nhl":          "icehockey_nhl",
    "mls":          "soccer_usa_mls",
    "ncaab":        "basketball_ncaab",
    "ncaaf":        "americanfootball_ncaaf",
    "wnba":         "basketball_wnba",
    # International soccer
    "epl":          "soccer_epl",
    "laliga":       "soccer_spain_la_liga",
    "bundesliga":   "soccer_germany_bundesliga",
    "seriea":       "soccer_italy_serie_a",
    "ligue1":       "soccer_france_ligue_one",
    "ucl":          "soccer_uefa_champs_league",
    "uel":          "soccer_uefa_europa_league",
    "ligamx":       "soccer_mexico_ligamx",
    "brasileirao":  "soccer_brazil_campeonato",
    "eredivisie":   "soccer_netherlands_eredivisie",
    "premierleague_aus": "soccer_australia_aleague",
    # UFC / MMA
    "ufc":          "mma_mixed_martial_arts",
    # Tennis (major tournaments)
    "wimbledon":    "tennis_atp_wimbledon",
    "usopen_tennis": "tennis_atp_us_open",
    "frenchopen":   "tennis_atp_french_open",
    "ausopen":      "tennis_atp_aus_open",
    "wta_wimbledon": "tennis_wta_wimbledon",
    "wta_usopen":   "tennis_wta_us_open",
    "wta_frenchopen": "tennis_wta_french_open",
    "wta_ausopen":  "tennis_wta_aus_open",
    # Boxing
    "boxing":       "boxing_boxing",
    # Cricket
    "cricket_odi":  "cricket_odi",
    "cricket_test": "cricket_test_match",
    # Rugby
    "nrl":          "rugbyleague_nrl",
    "superrugby":   "rugbyunion_super_rugby",
}

# Sport keyword aliases for detection
_SPORT_KEYWORDS = {
    # US leagues
    "nba":          ["nba", " nba ", "basketball"],
    "nfl":          ["nfl", " nfl ", "quarterback", "superbowl", "super bowl"],
    "mlb":          ["mlb", " mlb ", "baseball", "pitcher", "world series"],
    "nhl":          ["nhl", " nhl ", "stanley cup"],
    "mls":          [" mls ", "soccer_usa", "sporting kc", "nycfc", "lafc", "sounders",
                     "revolution", "red bulls"],
    "ncaab":        ["ncaab", "college basketball", "march madness"],
    "ncaaf":        ["ncaaf", "college football", "cfp"],
    "wnba":         ["wnba", "women's basketball"],
    # International soccer — check before generic "soccer" to avoid MLS collision
    "epl":          ["premier league", "epl", "man city", "man united", "arsenal",
                     "chelsea", "liverpool", "tottenham", "newcastle", "aston villa"],
    "laliga":       ["la liga", "laliga", "real madrid", "barcelona", "atletico madrid",
                     "sevilla", "real sociedad"],
    "bundesliga":   ["bundesliga", "bayern munich", "borussia dortmund", "rb leipzig",
                     "bayer leverkusen"],
    "seriea":       ["serie a", "juventus", "inter milan", "ac milan", "napoli", "roma",
                     "lazio"],
    "ligue1":       ["ligue 1", "ligue1", "psg", "paris saint-germain", "marseille",
                     "lyon", "monaco"],
    "ucl":          ["champions league", "ucl", "uefa champions"],
    "uel":          ["europa league", "uel", "uefa europa"],
    "ligamx":       ["liga mx", "ligamx", "club america", "chivas", "cruz azul",
                     "tigres", "monterrey"],
    "brasileirao":  ["brasileirao", "brasileirão", "flamengo", "palmeiras", "corinthians",
                     "são paulo", "santos"],
    "eredivisie":   ["eredivisie", "ajax", "psv", "feyenoord"],
    # UFC / MMA
    "ufc":          ["ufc", "mma", "mixed martial arts", "octagon", "bellator",
                     "one championship"],
    # Tennis
    "wimbledon":    ["wimbledon", "atp wimbledon", "wta wimbledon"],
    "usopen_tennis": ["us open tennis", "usta", "flushing meadows"],
    "frenchopen":   ["french open", "roland garros", "roland-garros"],
    "ausopen":      ["australian open", "aus open", "atp aus"],
    "wta_wimbledon": ["wta wimbledon"],
    "wta_usopen":   ["wta us open"],
    "wta_frenchopen": ["wta french open", "wta roland garros"],
    "wta_ausopen":  ["wta australian open"],
    # Boxing
    "boxing":       ["boxing", "heavyweight", "welterweight", "lightweight bout",
                     "wbc", "wba", "ibf title"],
    # Cricket
    "cricket_odi":  ["cricket", "odi", "t20", "ipl", "test match"],
    "cricket_test": ["test cricket", "ashes"],
    # Rugby
    "nrl":          ["nrl", "rugby league", "state of origin"],
    "superrugby":   ["super rugby", "rugby union", "six nations", "all blacks",
                     "springboks"],
}


def _detect_sport(market: dict) -> str:
    """Return sport key from market tags/slug/question, or empty string."""
    tags = " ".join(
        str(t).lower() if isinstance(t, str)
        else (t.get("label") or t.get("name") or "").lower()
        for t in (market.get("tags") or [])
    )
    slug     = (market.get("eventSlug") or market.get("slug") or "").lower()
    question = (market.get("question") or "").lower()
    combined = f"{tags} {slug} {question}"

    # Check specific leagues first so "premier league" beats generic "soccer",
    # "wimbledon" beats generic "tennis", etc.
    priority = [
        "epl", "laliga", "bundesliga", "seriea", "ligue1", "ucl", "uel",
        "ligamx", "brasileirao", "eredivisie",
        "wimbledon", "frenchopen", "usopen_tennis", "ausopen",
        "wta_wimbledon", "wta_frenchopen", "wta_usopen", "wta_ausopen",
        "ufc", "boxing", "cricket_test", "cricket_odi",
        "nrl", "superrugby",
        "nba", "nfl", "mlb", "nhl", "mls", "ncaab", "ncaaf", "wnba",
    ]
    for sport in priority:
        keywords = _SPORT_KEYWORDS.get(sport, [])
        if any(kw in combined for kw in keywords):
            return sport
    return ""


def _extract_teams(question: str) -> tuple[str, str]:
    """
    Extract two team names from a Polymarket question.
    Returns (focus_team, opponent) or ("", "") if parsing fails.
    """
    q = question.strip()

    # "X vs Y" / "X v Y"
    m = re.search(r'^(.+?)\s+v(?:s\.?|ersus)\s+(.+?)(?:\s*[:\-?]|$)', q, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # "Will [the] TEAM beat/defeat TEAM"
    m = re.search(
        r'will\s+(?:the\s+)?(.+?)\s+(?:beat|defeat|win\s+(?:against|vs\.?))\s+(?:the\s+)?(.+?)(?:\?|$)',
        q, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # "Will [the] TEAM win" (single-team question)
    m = re.search(r'will\s+(?:the\s+)?(.+?)\s+win', q, re.IGNORECASE)
    if m:
        return m.group(1).strip(), ""

    # "TEAM to win"
    m = re.search(r'^(.+?)\s+to\s+win', q, re.IGNORECASE)
    if m:
        return m.group(1).strip(), ""

    return "", ""


def _token_overlap(a: str, b: str) -> float:
    """Fraction of tokens in the shorter string that appear in the longer."""
    stop = {"the", "fc", "sc", "city", "united", "athletic"}
    ta = set(re.sub(r'[^\w\s]', '', a).lower().split()) - stop
    tb = set(re.sub(r'[^\w\s]', '', b).lower().split()) - stop
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _find_game(games: list[dict], team_a: str, team_b: str) -> Optional[dict]:
    """Fuzzy-match a game by team names. Returns best match or None."""
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
    """
    Average h2h moneyline probabilities across all bookmakers, vig-removed.
    Returns {team_name: probability} dict.
    """
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
    """Fetch odds for a sport, caching for 6 hours to conserve API quota."""
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
    """
    Compare Polymarket price against bookmaker consensus odds.

    Returns (edge, side) where:
      edge = abs(bookmaker_prob - poly_price)  [e.g. 0.12 = 12%]
      side = "YES" if bookmaker thinks YES is underpriced, "NO" otherwise

    Returns None if no key, no sport match, no game match, or edge below threshold.
    """
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
        logger.debug("No h2h bookmaker odds for %s vs %s", game.get("home_team"), game.get("away_team"))
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
