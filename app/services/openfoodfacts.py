"""Open Food Facts integration.

Open Food Facts limits each IP to 15 product reads and 10 searches per minute.
Every call from this API comes from one Render IP, so searches are cached and
throttled here, and callers must treat "not available" as a normal result.
"""

import time
from threading import Lock
from urllib.parse import urlparse

import httpx

from app.config import get_settings

# Stay under the published 10 searches/minute, leaving headroom for retries.
_SEARCH_LIMIT_PER_MINUTE = 8
_SEARCH_CACHE_TTL_SECONDS = 12 * 60 * 60
_SEARCH_CACHE_MAX = 2_000

_search_times: list[float] = []
_search_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_search_lock = Lock()


def extract_domain(website: str) -> str | None:
    """Extract clean domain from website URL."""

    try:
        parsed = urlparse(website)
        domain = parsed.netloc
        if domain.startswith("www."):
            domain = domain[4:]
        return domain if domain else None
    except Exception:
        return None


def _open_food_facts_host(country_code: str) -> str:
    """Return the Open Food Facts host for a country code.

    Country subdomains (us., tr., gb.) scope results to products sold there.
    """

    code = (country_code or "US").strip().lower()
    if code == "gb":
        code = "uk"
    if not code.isalpha() or len(code) != 2:
        return "world.openfoodfacts.org"
    return f"{code}.openfoodfacts.org"


def _headers() -> dict[str, str]:
    return {"User-Agent": get_settings().OPEN_FOOD_FACTS_USER_AGENT}


def _categories(product: dict) -> tuple[list[str], str | None]:
    """Return (category tags ordered general to specific, primary category)."""

    tags = [tag for tag in (product.get("categories_hierarchy") or product.get("categories_tags") or []) if isinstance(tag, str)]
    primary = product.get("compared_to_category")
    if not isinstance(primary, str) or not primary:
        primary = tags[-1] if tags else None
    return tags, primary


async def lookup_barcode(barcode: str, country_code: str = "US") -> dict:
    """Look up a product by barcode in Open Food Facts."""

    normalized_country = (country_code or "US").strip().upper()
    url = f"https://{_open_food_facts_host(normalized_country)}/api/v2/product/{barcode}.json"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, headers=_headers())
        if response.status_code == 404:
            return {"found": False, "barcode": barcode, "country_code": normalized_country}
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return {"found": False, "barcode": barcode, "country_code": normalized_country}

    if payload.get("status") != 1 or not payload.get("product"):
        return {"found": False, "barcode": barcode, "country_code": normalized_country}

    product = payload["product"]
    contact = product.get("contact") or {}
    contact_email = contact.get("email") if isinstance(contact, dict) else None
    categories, primary_category = _categories(product)
    return {
        "found": True,
        "name": product.get("product_name") or product.get("generic_name") or "Unknown product",
        "brand": product.get("brands"),
        "ingredients_text": product.get("ingredients_text") or "",
        "image_url": product.get("image_url"),
        "website": product.get("link") or product.get("website") or None,
        "contact_email": contact_email,
        "barcode": barcode,
        "country_code": normalized_country,
        "categories": categories,
        "primary_category": primary_category,
        "labels": [tag for tag in product.get("labels_tags") or [] if isinstance(tag, str)],
    }


def _take_search_slot() -> bool:
    """Reserve one search call within the per-minute budget."""

    now = time.monotonic()
    with _search_lock:
        while _search_times and now - _search_times[0] > 60:
            _search_times.pop(0)
        if len(_search_times) >= _SEARCH_LIMIT_PER_MINUTE:
            return False
        _search_times.append(now)
        return True


async def search_halal_labeled(category: str, country_code: str = "US", limit: int = 10) -> list[dict] | None:
    """Return products in a category that carry a halal label on Open Food Facts.

    The halal label is the manufacturer's or certifier's claim as recorded by
    Open Food Facts contributors; it is not a tayyib verdict.

    Returns None when the search could not run (throttled or unavailable), so
    callers can tell "no results" apart from "try again later".
    """

    key = (category, (country_code or "US").upper())
    now = time.monotonic()
    with _search_lock:
        cached = _search_cache.get(key)
        if cached and now - cached[0] < _SEARCH_CACHE_TTL_SECONDS:
            return cached[1]

    if not _take_search_slot():
        return None

    params = {
        "categories_tags": category,
        "labels_tags": "en:halal",
        "fields": "code,product_name,brands,image_small_url,image_front_small_url,categories_tags,labels_tags",
        "sort_by": "unique_scans_n",
        "page_size": str(max(1, min(limit, 24))),
    }
    url = f"https://{_open_food_facts_host(key[1])}/api/v2/search"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, params=params, headers=_headers())
        if response.status_code in (429, 503):
            return None
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None

    results = []
    for item in payload.get("products") or []:
        code = str(item.get("code") or "").strip()
        name = (item.get("product_name") or "").strip()
        if not code or not name:
            continue
        results.append(
            {
                "barcode": code,
                "name": name,
                "brand": (item.get("brands") or "").split(",")[0].strip() or None,
                "image_url": item.get("image_front_small_url") or item.get("image_small_url"),
                "categories": [tag for tag in item.get("categories_tags") or [] if isinstance(tag, str)],
            }
        )

    with _search_lock:
        if len(_search_cache) >= _SEARCH_CACHE_MAX:
            _search_cache.clear()
        _search_cache[key] = (now, results)
    return results
