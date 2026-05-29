"""Open Food Facts integration."""

from urllib.parse import urlparse

import httpx

from app.config import get_settings


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


async def lookup_barcode(barcode: str) -> dict:
    """Look up a product by barcode in Open Food Facts."""

    settings = get_settings()
    url = f"https://world.openfoodfacts.org/api/v2/product/{barcode}.json"
    headers = {"User-Agent": settings.OPEN_FOOD_FACTS_USER_AGENT}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, headers=headers)
        if response.status_code == 404:
            return {"found": False, "barcode": barcode}
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return {"found": False, "barcode": barcode}

    if payload.get("status") != 1 or not payload.get("product"):
        return {"found": False, "barcode": barcode}

    product = payload["product"]
    contact = product.get("contact") or {}
    contact_email = contact.get("email") if isinstance(contact, dict) else None
    return {
        "found": True,
        "name": product.get("product_name") or product.get("generic_name") or "Unknown product",
        "brand": product.get("brands"),
        "ingredients_text": product.get("ingredients_text") or "",
        "image_url": product.get("image_url"),
        "website": product.get("link") or product.get("website") or None,
        "contact_email": contact_email,
        "barcode": barcode,
    }
