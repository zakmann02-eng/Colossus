"""
Weather forecast integration for temperature/weather market trading.

Fetches daily high/low temperature forecasts from Open-Meteo (free, no API key)
and compares to the Polymarket price to find edge.

Returns (edge, side) when the forecast probability diverges by >= MIN_EDGE
from the Polymarket YES price, else None.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import aiohttp

logger = logging.getLogger(__name__)

MIN_EDGE  = 0.05   # 5% minimum edge — slightly higher than sports due to forecast uncertainty
_CACHE_TTL = 3600.0  # 1 hour — forecasts don't change minute-to-minute

# City name (lowercase) → (latitude, longitude, timezone)
_CITIES: dict[str, tuple[float, float, str]] = {
    "miami":         (25.77,  -80.19, "America/New_York"),
    "new york":      (40.71,  -74.01, "America/New_York"),
    "nyc":           (40.71,  -74.01, "America/New_York"),
    "new york city": (40.71,  -74.01, "America/New_York"),
    "los angeles":   (34.05, -118.24, "America/Los_Angeles"),
    "la":            (34.05, -118.24, "America/Los_Angeles"),
    "chicago":       (41.88,  -87.63, "America/Chicago"),
    "dallas":        (32.78,  -96.80, "America/Chicago"),
    "atlanta":       (33.75,  -84.39, "America/New_York"),
    "seattle":       (47.61, -122.33, "America/Los_Angeles"),
    "denver":        (39.74, -104.98, "America/Denver"),
    "phoenix":       (33.45, -112.07, "America/Phoenix"),
    "boston":        (42.36,  -71.06, "America/New_York"),
    "houston":       (29.76,  -95.37, "America/Chicago"),
    "las vegas":     (36.17, -115.14, "America/Los_Angeles"),
    "san francisco": (37.77, -122.42, "America/Los_Angeles"),
    "sf":            (37.77, -122.42, "America/Los_Angeles"),
    "washington":    (38.91,  -77.04, "America/New_York"),
    "minneapolis":   (44.98,  -93.27, "America/Chicago"),
    "new orleans":   (29.95,  -90.07, "America/Chicago"),
    "orlando":       (28.54,  -81.38, "America/New_York"),
    "tampa":         (27.95,  -82.46, "America/New_York"),
    "charlotte":     (35.23,  -80.84, "America/New_York"),
    "nashville":     (36.17,  -86.78, "America/Chicago"),
    "kansas city":   (39.10,  -94.58, "America/Chicago"),
    "jacksonville":  (30.33,  -81.66, "America/New_York"),
    "memphis":       (35.15,  -90.05, "America/Chicago"),
    "baltimore":     (39.29,  -76.61, "America/New_York"),
    "oklahoma city": (35.47,  -97.52, "America/Chicago"),
    "louisville":    (38.25,  -85.76, "America/New_York"),
    "portland":      (45.52, -122.68, "America/Los_Angeles"),
    "sacramento":    (38.58, -121.49, "America/Los_Angeles"),
    "san antonio":   (29.42,  -98.49, "America/Chicago"),
    "san diego":     (32.72, -117.16, "America/Los_Angeles"),
    "san jose":      (37.34, -121.89, "America/Los_Angeles"),
    "cleveland":     (41.50,  -81.69, "America/New_York"),
    "pittsburgh":    (40.44,  -79.99, "America/New_York"),
    "detroit":       (42.33,  -83.05, "America/New_York"),
    "cincinnati":    (39.10,  -84.51, "America/New_York"),
    "st. louis":     (38.63,  -90.20, "America/Chicago"),
    "st louis":      (38.63,  -90.20, "America/Chicago"),
    "salt lake city":(40.76, -111.89, "America/Denver"),
    "indianapolis":  (39.77,  -86.16, "America/Indiana/Indianapolis"),
    "milwaukee":     (43.04,  -87.91, "America/Chicago"),
    "columbus":      (39.96,  -82.99, "America/New_York"),
    "austin":        (30.27,  -97.74, "America/Chicago"),
    "raleigh":       (35.77,  -78.64, "America/New_York"),
    "richmond":      (37.54,  -77.43, "America/New_York"),
    "anchorage":     (61.22, -149.90, "America/Anchorage"),
    "honolulu":      (21.31, -157.86, "Pacific/Honolulu"),
}

_forecast_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = asyncio.Lock()


def _parse_weather_market(market: dict) -> tuple[str, float, str] | None:
    """
    Parse a weather/temperature market question to extract:
        (city, threshold_fahrenheit, direction)

    direction: "above"  — market resolves YES if temperature exceeds threshold
               "below"  — market resolves YES if temperature stays below threshold

    Returns None if unable to parse city or threshold.
    """
    text = " ".join([
        (market.get("question") or market.get("title") or "").lower(),
        (market.get("slug") or "").lower(),
        (market.get("eventSlug") or "").lower(),
    ])

    # Extract numeric temperature threshold
    # Matches: 95°F, 95°, 95 degrees, 95f, 100f, etc.
    m = re.search(r'\b(\d{2,3})\s*(?:°\s*f|°|degrees?\s*(?:fahrenheit)?|f(?=\W|$))', text)
    if not m:
        return None
    threshold = float(m.group(1))
    # Sanity-check: typical daily temperature range
    if not (0 <= threshold <= 130):
        return None

    # Determine direction
    above_kw = ("reach", "exceed", "above", "at least", "hit", "or above",
                 "or higher", "or more", "top out", "break")
    below_kw = ("below", "under", "not reach", "stay below", "fail to reach",
                 "less than", "or less", "or lower")
    direction = "above"
    for kw in below_kw:
        if kw in text:
            direction = "below"
            break

    # Match city — longest name first to avoid "la" matching inside "las vegas"
    for city_name in sorted(_CITIES.keys(), key=len, reverse=True):
        if city_name in text:
            return city_name, threshold, direction

    return None


async def _fetch_forecast(city: str, session: aiohttp.ClientSession) -> dict | None:
    """Fetch 3-day daily high/low forecast for a city from Open-Meteo (free, no API key)."""
    coords = _CITIES.get(city)
    if not coords:
        return None
    lat, lon, tz = coords

    async with _cache_lock:
        cached = _forecast_cache.get(city)
        if cached and time.time() - cached[0] < _CACHE_TTL:
            return cached[1]

    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&daily=temperature_2m_max,temperature_2m_min"
        f"&temperature_unit=fahrenheit"
        f"&timezone={tz.replace('/', '%2F')}"
        f"&forecast_days=3"
    )
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json()
    except Exception as exc:
        logger.debug("Open-Meteo %s fetch failed: %s", city, exc)
        return None

    async with _cache_lock:
        _forecast_cache[city] = (time.time(), data)
    return data


def _forecast_probability(forecast_val: float, threshold: float, direction: str) -> float:
    """
    Convert a deterministic forecast value into a probability that the
    market's threshold will be met.

    Uses a piecewise linear heuristic calibrated on NWS forecast accuracy:
      ±0°F from threshold → 55% (essentially coin-flip — forecast uncertainty)
      ±5°F                → 85%/20%
      ±10°F               → 95%/5%
    """
    margin = forecast_val - threshold  # positive = forecast above threshold

    if direction == "above":
        if margin >= 10:   return 0.95
        if margin >= 5:    return 0.85
        if margin >= 2:    return 0.70
        if margin >= 0:    return 0.57
        if margin >= -2:   return 0.43
        if margin >= -5:   return 0.20
        return 0.05
    else:  # below threshold
        if margin <= -10:  return 0.95
        if margin <= -5:   return 0.85
        if margin <= -2:   return 0.70
        if margin <= 0:    return 0.57
        if margin <= 2:    return 0.43
        if margin <= 5:    return 0.20
        return 0.05


async def get_weather_signal(
    market: dict,
    polymarket_price: float,
    session: aiohttp.ClientSession,
) -> tuple[float, str] | None:
    """
    Compare Open-Meteo temperature forecast to the Polymarket YES price.

    Returns (edge, side) when |forecast_prob - price| >= MIN_EDGE, else None.
    edge  — size of the pricing discrepancy (absolute)
    side  — "YES" if market is underpriced, "NO" if overpriced
    """
    parsed = _parse_weather_market(market)
    if parsed is None:
        logger.debug("Weather market parse failed: q=%s slug=%s",
                     (market.get("question") or "")[:60],
                     (market.get("slug") or "")[:40])
        return None

    city, threshold, direction = parsed

    forecast = await _fetch_forecast(city, session)
    if forecast is None:
        logger.info("T6-no-forecast: city=%s q=%s", city,
                    (market.get("question") or "")[:60])
        return None

    try:
        daily   = forecast.get("daily", {})
        highs   = daily.get("temperature_2m_max", [])
        lows    = daily.get("temperature_2m_min", [])
        if not highs:
            return None
        forecast_high = float(highs[0])
        forecast_low  = float(lows[0]) if lows else forecast_high
    except Exception as exc:
        logger.debug("Weather forecast parse error: %s", exc)
        return None

    forecast_val = forecast_high if direction == "above" else forecast_low
    prob = _forecast_probability(forecast_val, threshold, direction)

    edge = prob - polymarket_price  # positive → YES underpriced, negative → YES overpriced

    logger.info(
        "T6-weather: city=%s threshold=%.0f°F dir=%s forecast=%.1f°F prob=%.2f price=%.2f edge=%+.2f q=%s",
        city, threshold, direction, forecast_val, prob,
        polymarket_price, edge,
        (market.get("question") or "")[:50],
    )

    if abs(edge) < MIN_EDGE:
        return None

    return abs(edge), ("YES" if edge > 0 else "NO")
