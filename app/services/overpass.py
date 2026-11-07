"""Fetch halal places to eat from OpenStreetMap via the Overpass API.

The public Overpass servers ask for roughly 10,000 requests a day at most.
Results are cached by ~1 km grid cell for 12 hours and calls are throttled,
so normal app use stays far below that. Data is © OpenStreetMap contributors
(ODbL) and must be credited wherever it is shown.
"""

import time
from threading import Lock

import httpx

from app.config import get_settings

ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
CACHE_TTL_SECONDS = 12 * 60 * 60
CACHE_MAX = 5_000
CALLS_PER_MINUTE = 6

_cache: dict[tuple[float, float, int], tuple[float, list[dict]]] = {}
_calls: list[float] = []
_lock = Lock()

QUERY = """
[out:json][timeout:25];
nwr["amenity"~"^(restaurant|fast_food|cafe|food_court)$"]["diet:halal"~"^(yes|only)",i](around:{radius},{lat},{lng});
out center tags 400;
"""


def _key(lat: float, lng: float, radius_m: int) -> tuple[float, float, int]:
    # ~1.1 km cells; radius rounded up to the next 5 km so nearby users share results.
    bucket = ((radius_m + 4_999) // 5_000) * 5_000
    return (round(lat, 2), round(lng, 2), bucket)


def _take_slot() -> bool:
    now = time.monotonic()
    with _lock:
        while _calls and now - _calls[0] > 60:
            _calls.pop(0)
        if len(_calls) >= CALLS_PER_MINUTE:
            return False
        _calls.append(now)
        return True


async def fetch_halal_food(lat: float, lng: float, radius_m: int) -> list[dict] | None:
    """Raw Overpass elements around a point, or None if OSM can't be reached now.

    The query covers the whole cached cell, so callers must still filter by
    their own distance.
    """

    key = _key(lat, lng, radius_m)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]

    if not _take_slot():
        return None

    query = QUERY.format(radius=key[2] + 1_500, lat=key[0], lng=key[1])
    headers = {"User-Agent": get_settings().OPEN_FOOD_FACTS_USER_AGENT}
    for url in ENDPOINTS:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(url, data={"data": query}, headers=headers)
            if response.status_code in (429, 503, 504):
                continue
            response.raise_for_status()
            elements = response.json().get("elements") or []
        except Exception:
            continue
        with _lock:
            if len(_cache) >= CACHE_MAX:
                _cache.clear()
            _cache[key] = (now, elements)
        return elements
    return None
