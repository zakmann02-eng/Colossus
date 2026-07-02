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

MIN_EDGE   = 0.03         # 3% minimum edge to fire T5
_CACHE_TTL = 2 * 3600.0   # 2 hours — keeps live-game odds fresh

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
    "tennis":       "tennis_wta",
    "tennis_atp":   "tennis_atp",
    "tennis_wta":   "tennis_wta",
    "cricket":      "cricket_test_match",
    "golf":         "golf_pga_tour",
    "rugby":        "rugbyleague_nrl",
    "aussierules":  "aussierules_afl",
    "worldcup":     "soccer_fifa_world_cup",
    "copa_america": "soccer_conmebol_copa_america",
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
                   "world cup 2026", "2026 world cup", "fifa wc", "wc 2026",
                   "fwc", "atc-fwc"],
    "ufc":        ["ufc", "ultimate fighting championship"],
    "mma":        ["mma", "bellator", "one championship", "pfl"],
    "boxing":     ["boxing", "wbc", "wba", "ibf", "wbo", "heavyweight fight",
                   "lightweight fight", "welterweight"],
    "tennis_atp": ["atp", "itf mens", "itf men", "wimbledon men",
                   "french open men", "us open men", "australian open men"],
    "tennis_wta": ["wta", "itf womens", "itf women", "wimbledon women",
                   "french open women", "us open women", "australian open women",
                   "grass court championships"],
    "tennis":     ["tennis", "wimbledon", "roland garros"],
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
    "copa_america": ["copa america", "conmebol copa america", "copa america 2026"],
    "copa":         ["copa libertadores", "copa sudamericana"],
}


# ── Top players per sport (for player prop T5 signals) ────────────────────────
_TOP_PLAYERS: dict[str, list[str]] = {
    "basketball_nba": [
        "lebron james", "stephen curry", "kevin durant", "nikola jokic",
        "luka doncic", "jayson tatum", "giannis antetokounmpo", "joel embiid",
        "anthony davis", "devin booker", "damian lillard", "jimmy butler",
    ],
    "americanfootball_nfl": [
        "patrick mahomes", "josh allen", "lamar jackson", "jalen hurts",
        "justin jefferson", "tyreek hill", "travis kelce", "davante adams",
        "christian mccaffrey", "derrick henry", "cooper kupp",
    ],
    "baseball_mlb": [
        "shohei ohtani", "mike trout", "mookie betts", "aaron judge",
        "juan soto", "bryce harper", "freddie freeman", "ronald acuna",
    ],
    "icehockey_nhl": [
        "connor mcdavid", "leon draisaitl", "david pastrnak", "auston matthews",
        "nathan mackinnon", "alex ovechkin", "sidney crosby", "nikita kucherov",
    ],
    "soccer_fifa_world_cup": [
        "messi", "ronaldo", "mbappe", "haaland", "vinicius", "harry kane",
        "pedri", "yamal", "bellingham", "neymar", "salah",
    ],
    "tennis_atp": ["djokovic", "alcaraz", "sinner", "medvedev", "zverev"],
    "tennis_wta": ["swiatek", "sabalenka", "gauff", "rybakina"],
}

# Odds API player prop market keys per stat type
_PROP_MARKET_KEYS: dict[str, str] = {
    "points":          "player_points",
    "assists":         "player_assists",
    "rebounds":        "player_rebounds",
    "threes":          "player_threes",
    "passing yards":   "player_pass_yds",
    "rushing yards":   "player_rush_yds",
    "receiving yards": "player_reception_yds",
    "strikeouts":      "pitcher_strikeouts",
    "home runs":       "batter_home_runs",
    "goals":           "player_goal_scorer_anytime",
    "shots on target": "player_shots_on_target",
}

# ── Cache ──────────────────────────────────────────────────────────────────────

_cache: dict[str, tuple[float, list[dict]]] = {}
_prop_cache: dict[str, tuple[float, dict]] = {}  # event_id:market → (ts, data)
_cache_lock = asyncio.Lock()
_PROP_CACHE_TTL = 3600.0  # 1 hour for player props


def _detect_player_and_stat(question: str, sport_key: str) -> tuple[str, str] | None:
    """Return (player_name, stat_type) if question is a top-player prop, else None."""
    q = question.lower()
    for player in _TOP_PLAYERS.get(sport_key, []):
        if player in q:
            for stat_name in _PROP_MARKET_KEYS:
                if stat_name in q:
                    return player, stat_name
    return None


async def _fetch_player_prop_signal(
    sport_key: str,
    player: str,
    stat: str,
    polymarket_price: float,
    session: aiohttp.ClientSession,
) -> tuple[float, str] | None:
    """Fetch Odds API player prop odds and compare to Polymarket price."""
    api_key = os.getenv("ODDS_API_KEY", "").strip()
    if not api_key:
        return None

    prop_market = _PROP_MARKET_KEYS.get(stat)
    if not prop_market:
        return None

    events = await _fetch_sport_events(sport_key, session)
    if not events:
        return None

    for event in events[:8]:  # check up to 8 events to find the player
        event_id = event.get("id")
        if not event_id:
            continue

        cache_key = f"{event_id}:{prop_market}"
        async with _cache_lock:
            cached = _prop_cache.get(cache_key)
            if cached and time.time() - cached[0] < _PROP_CACHE_TTL:
                prop_data = cached[1]
            else:
                prop_data = None

        if prop_data is None:
            url = (
                f"https://api.the-odds-api.com/v4/sports/{sport_key}"
                f"/events/{event_id}/odds"
                f"?apiKey={api_key}&regions=us&markets={prop_market}&oddsFormat=decimal"
            )
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 404:
                        continue
                    if resp.status != 200:
                        logger.debug("Prop API HTTP %d for %s/%s", resp.status, event_id, prop_market)
                        continue
                    prop_data = await resp.json()
                    async with _cache_lock:
                        _prop_cache[cache_key] = (time.time(), prop_data)
            except Exception as exc:
                logger.debug("Prop API error: %s", exc)
                continue

        # Search bookmakers for this player's prop
        probs: list[float] = []
        for bm in (prop_data or {}).get("bookmakers", []):
            for mkt in bm.get("markets", []):
                for outcome in mkt.get("outcomes", []):
                    desc = (outcome.get("description") or outcome.get("name") or "").lower()
                    if player not in desc:
                        continue
                    try:
                        price = float(outcome.get("price") or 0)
                        if price >= 1.0:
                            probs.append(1.0 / price)
                    except (TypeError, ValueError):
                        pass

        if not probs:
            continue

        bm_prob = sum(probs) / len(probs)
        bm_prob = max(0.01, min(0.99, bm_prob))
        edge = bm_prob - polymarket_price

        if abs(edge) < MIN_EDGE:
            return None

        side = "YES" if edge > 0 else "NO"
        logger.info(
            "T5 player prop: %s | %s %s | bm=%.3f poly=%.3f edge=%.2f%% side=%s",
            player, stat, event.get("id", "")[:8], bm_prob, polymarket_price, abs(edge) * 100, side,
        )
        return abs(edge), side

    return None


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
    """
    Average implied probability across all bookmakers for the given outcome name.
    target is matched case-insensitively anywhere in the outcome name.
    """
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
                logger.info("Odds API: fetched %d events for %s", len(data), sport_key)
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
    """
    Try to match a Polymarket question to an Odds API event.
    Looks for team/player name overlap.
    """
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
        # Partial word match
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

    slug = market.get("slug") or market.get("eventSlug") or ""
    text_for_detection = f"{question} {slug}"
    sport = _detect_sport(text_for_detection)
    if not sport:
        return None

    sport_key = _SPORT_KEYS.get(sport)
    if not sport_key:
        return None

    logger.info("T5 attempting: sport=%s key=%s q=%s", sport, sport_key, question[:50])

    # Player prop path — check if this is a top-player stat market
    prop_info = _detect_player_and_stat(question, sport_key)
    if prop_info:
        player, stat = prop_info
        logger.debug("Player prop detected: %s | %s | %s", player, stat, question[:50])
        return await _fetch_player_prop_signal(sport_key, player, stat, polymarket_price, session)

    events = await _fetch_sport_events(sport_key, session)
    if not events:
        logger.info("T5: no events returned from Odds API for sport_key=%s", sport_key)
        return None

    event = _find_matching_event(events, question)
    if not event:
        logger.info("T5: no event match in %d events for: %s", len(events), question[:60])
        return None

    # Determine which team/player the Polymarket question is asking about
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

    # Per-bookmaker vig-normalized consensus probability.
    # For each bookmaker's h2h market: divide target team's implied prob by
    # that bookmaker's total overround. Average normalized probs across bookmakers.
    # This correctly handles 2-way and 3-way markets without inflating edges.
    bm_probs: list[float] = []
    target_l = target_team.lower()
    for bm in event.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt.get("key") != "h2h":
                continue
            total_implied = 0.0
            target_implied: float | None = None
            for outcome in mkt.get("outcomes", []):
                price_raw = outcome.get("price")
                if price_raw is None:
                    continue
                try:
                    p = float(price_raw)
                    impl = 1.0 / p if p >= 1.0 else p
                    total_implied += impl
                    name = (outcome.get("name") or "").lower()
                    if target_l in name or name in target_l:
                        target_implied = impl
                except (TypeError, ValueError):
                    pass
            if target_implied is not None and total_implied > 1.0:
                bm_probs.append(target_implied / total_implied)

    if not bm_probs:
        logger.debug("Odds API: no odds for %s in event %s", target_team, event.get("id"))
        return None

    bm_prob = sum(bm_probs) / len(bm_probs)
    bm_prob = max(0.01, min(0.99, bm_prob))
    edge = bm_prob - polymarket_price

    if abs(edge) < MIN_EDGE:
        logger.info(
            "T5: edge %.2f%% below %.0f%% threshold for %s (bm=%.3f poly=%.3f)",
            abs(edge) * 100, MIN_EDGE * 100, question[:50], bm_prob, polymarket_price,
        )
        return None

    side = "YES" if edge > 0 else "NO"
    logger.info(
        "T5 signal: %s | bm_prob=%.3f poly_price=%.3f edge=%.2f%% side=%s",
        question[:50], bm_prob, polymarket_price, abs(edge) * 100, side,
    )
    return abs(edge), side
