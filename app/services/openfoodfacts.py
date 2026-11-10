"""Open Food Facts integration.

Open Food Facts limits each IP to 15 product reads and 10 searches per minute,
and going over can get the IP banned. Every call from this API comes from one
Render IP, so reads and searches are budgeted here, misses are cached, and
callers must treat "busy" and "not available" as normal results.
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

# Product reads: published limit is 15/minute per IP, and going over risks a
# ban of the server's only IP. Stay at 12 and hand the rest to the user's
# device (Open Food Facts applies limits per user for app traffic).
_PRODUCT_LIMIT_PER_MINUTE = 12
_product_times: list[float] = []
_product_lock = Lock()
_backoff_until = 0.0
# Barcodes Open Food Facts doesn't have, so repeat scans don't spend reads.
_MISS_TTL_SECONDS = 6 * 60 * 60
_MISS_MAX = 5_000
_misses: dict[str, float] = {}


def _take_product_slot() -> bool:
    """Reserve one product read within the per-minute budget."""

    now = time.monotonic()
    with _product_lock:
        if now < _backoff_until:
            return False
        while _product_times and now - _product_times[0] > 60:
            _product_times.pop(0)
        if len(_product_times) >= _PRODUCT_LIMIT_PER_MINUTE:
            return False
        _product_times.append(now)
        return True


def _back_off(seconds: float = 60) -> None:
    global _backoff_until
    with _product_lock:
        _backoff_until = time.monotonic() + seconds


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


def normalize_product(product: dict, barcode: str, country_code: str) -> dict:
    """Turn an Open Food Facts product object into the shape the scan flow uses."""

    contact = product.get("contact") or {}
    contact_email = contact.get("email") if isinstance(contact, dict) else None
    categories, primary_category = _categories(product)

    def text(key: str, limit: int) -> str | None:
        value = product.get(key)
        return value.strip()[:limit] if isinstance(value, str) and value.strip() else None

    return {
        "found": True,
        "name": text("product_name", 200) or text("generic_name", 200) or "Unknown product",
        "brand": text("brands", 200),
        "ingredients_text": text("ingredients_text", 5000) or "",
        "image_url": text("image_url", 500),
        "website": text("link", 500) or text("website", 500),
        "contact_email": contact_email if isinstance(contact_email, str) else None,
        "barcode": barcode,
        "country_code": country_code,
        "categories": categories[:40],
        "primary_category": primary_category,
        "labels": [tag for tag in product.get("labels_tags") or [] if isinstance(tag, str)][:60],
    }


async def lookup_barcode(barcode: str, country_code: str = "US") -> dict:
    """Look up a product by barcode in Open Food Facts.

    Returns {"found": False, "busy": True} when the read budget is spent or
    Open Food Facts asks us to slow down; the app then looks it up itself.
    """

    normalized_country = (country_code or "US").strip().upper()
    missing = {"found": False, "barcode": barcode, "country_code": normalized_country}
    now = time.monotonic()
    seen = _misses.get(barcode)
    if seen and now - seen < _MISS_TTL_SECONDS:
        return missing
    if not _take_product_slot():
        return {**missing, "busy": True}

    url = f"https://{_open_food_facts_host(normalized_country)}/api/v2/product/{barcode}.json"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, headers=_headers())
        if response.status_code in (429, 503):
            _back_off(60)
            return {**missing, "busy": True}
        if response.status_code == 404:
            payload = {}
        else:
            response.raise_for_status()
            payload = response.json()
    except httpx.TimeoutException:
        return {**missing, "busy": True}
    except Exception:
        # We couldn't ask; this is not Open Food Facts saying the product doesn't exist.
        return {**missing, "error": True}

    if payload.get("status") != 1 or not payload.get("product"):
        if len(_misses) >= _MISS_MAX:
            _misses.clear()
        _misses[barcode] = now
        return missing

    return normalize_product(payload["product"], barcode, normalized_country)


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
