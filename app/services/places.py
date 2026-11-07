"""Decide which map results are real halal places to eat.

Pure functions (no I/O) so the rules can be unit tested.

Rules for OpenStreetMap results:
- Must be a place to eat: amenity restaurant, fast_food, cafe or food_court.
- Must be tagged diet:halal=only (fully halal) or diet:halal=yes (serves
  halal). "limited" (a few halal items) and "no" are excluded.
- Anything also tagged as a shop is excluded, so groceries, butchers and
  supermarkets with a hot counter never show up.
- Names that read like a market or butcher are excluded unless the name also
  says it is somewhere to eat ("Market Grill" stays, "Halal Meat Market" goes).
- Disused, abandoned, private or unnamed places are excluded.
"""

from __future__ import annotations

import re
from math import asin, cos, radians, sin, sqrt

FOOD_AMENITIES = {
    "restaurant": "Restaurant",
    "fast_food": "Fast food",
    "cafe": "Café",
    "food_court": "Food court",
}

GROCERY_WORDS = re.compile(
    r"\b(market|supermarket|grocery|groceries|mart|minimart|butcher|butchery|meats?|"
    r"bazaa?r|store|wholesale|cash\s*&?\s*carry|halal\s+food\s+center)\b",
    re.IGNORECASE,
)
EATERY_WORDS = re.compile(
    r"\b(grill|grille|kitchen|restaurant|resto|cafe|café|coffee|kabob|kebab|kabab|pizza|pizzeria|"
    r"burger|burgers|shawarma|biryani|bistro|diner|eatery|chicken|wings|bbq|barbecue|gyro|gyros|"
    r"falafel|tandoori|taco|tacos|curry|noodles?|ramen|sushi|steakhouse|dhaba|mandi|express|cuisine)\b",
    re.IGNORECASE,
)


def distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Haversine distance in kilometres."""

    d_lat = radians(lat2 - lat1)
    d_lng = radians(lng2 - lng1)
    a = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lng / 2) ** 2
    return 2 * 6371 * asin(sqrt(a))


def looks_like_grocery(name: str) -> bool:
    """True for names like "Halal Meat Market"; False for "Market Grill"."""

    return bool(GROCERY_WORDS.search(name)) and not EATERY_WORDS.search(name)


def _cuisine(value: str | None) -> str | None:
    if not value:
        return None
    parts = [p.strip().replace("_", " ") for p in re.split(r"[;,]", value) if p.strip()]
    pretty = [p[:1].upper() + p[1:] for p in parts if p.lower() not in {"halal", "regional"}]
    return ", ".join(pretty[:3]) or None


def _address(tags: dict) -> str | None:
    street = " ".join(x for x in (tags.get("addr:housenumber"), tags.get("addr:street")) if x)
    parts = [p for p in (street, tags.get("addr:city")) if p]
    return ", ".join(parts) or None


def _website(tags: dict) -> str | None:
    url = (tags.get("website") or tags.get("contact:website") or "").strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    return url if re.match(r"^https?://[^\s/]+\.[^\s]+", url) else None


def classify_osm(element: dict, origin: tuple[float, float]) -> dict | None:
    """Turn one Overpass element into a map place, or None if it should not show."""

    tags = element.get("tags") or {}
    name = (tags.get("name") or "").strip()
    amenity = tags.get("amenity")
    halal = (tags.get("diet:halal") or "").strip().lower().split(";")[0]

    if not name or amenity not in FOOD_AMENITIES:
        return None
    if halal not in {"only", "yes"}:
        return None
    if tags.get("shop"):
        return None
    if any(tags.get(k) in {"yes", "true"} for k in ("disused", "abandoned")) or tags.get("access") == "private":
        return None
    if (tags.get("opening_hours") or "").strip().lower() in {"closed", "off"}:
        return None
    if looks_like_grocery(name):
        return None

    lat = element.get("lat", (element.get("center") or {}).get("lat"))
    lng = element.get("lon", (element.get("center") or {}).get("lon"))
    if lat is None or lng is None:
        return None

    certifier = next(
        (tags[k] for k in ("diet:halal:certification", "halal:certification", "certification:halal") if tags.get(k)),
        None,
    )
    return {
        "id": f"osm:{element.get('type', 'node')}/{element.get('id')}",
        "source": "osm",
        "name": name,
        "lat": float(lat),
        "lng": float(lng),
        "distance_km": round(distance_km(origin[0], origin[1], float(lat), float(lng)), 2),
        "kind": amenity,
        "kind_label": FOOD_AMENITIES[amenity],
        "cuisine": _cuisine(tags.get("cuisine")),
        "halal_level": "fully_halal" if halal == "only" else "halal",
        "certified": bool(certifier),
        "certifier": certifier,
        "address": _address(tags),
        "phone": tags.get("phone") or tags.get("contact:phone"),
        "website": _website(tags),
        "opening_hours": tags.get("opening_hours"),
        "prayer_space": None,
        "avg_rating": None,
        "review_count": 0,
    }


def from_listing(row: dict, origin: tuple[float, float]) -> dict | None:
    """Turn a reviewed tayyib. listing into a map place (unverified ones never show)."""

    status = (row.get("halal_status") or "unverified").lower()
    if status not in {"fully_halal", "halal_options"} or row.get("lat") is None or row.get("lng") is None:
        return None
    lat, lng = float(row["lat"]), float(row["lng"])
    return {
        "id": f"tayyib:{row['id']}",
        "source": "tayyib",
        "name": row.get("name"),
        "lat": lat,
        "lng": lng,
        "distance_km": round(distance_km(origin[0], origin[1], lat, lng), 2),
        "kind": "restaurant",
        "kind_label": "Restaurant",
        "cuisine": row.get("cuisine_type"),
        "halal_level": "fully_halal" if status == "fully_halal" else "halal_options",
        "certified": bool(row.get("certification_doc_url")),
        "certifier": None,
        "address": row.get("address"),
        "phone": row.get("phone"),
        "website": row.get("website"),
        "opening_hours": None,
        "prayer_space": bool(row.get("prayer_space")),
        "avg_rating": row.get("avg_rating"),
        "review_count": row.get("review_count") or 0,
    }


def _same_place(a: dict, b: dict) -> bool:
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())  # noqa: E731
    close = distance_km(a["lat"], a["lng"], b["lat"], b["lng"]) < 0.08
    return close and (norm(a["name"]) == norm(b["name"]) or norm(a["name"]) in norm(b["name"]) or norm(b["name"]) in norm(a["name"]))


def merge_places(
    listings: list[dict],
    osm: list[dict],
    hidden_ids: set[str],
    radius_km: float,
    limit: int = 150,
) -> list[dict]:
    """Our reviewed listings win over a matching OSM entry; nearest first."""

    merged = [p for p in listings if p["id"] not in hidden_ids]
    for place in osm:
        if place["id"] in hidden_ids or any(_same_place(place, kept) for kept in merged):
            continue
        merged.append(place)
    merged = [p for p in merged if p["distance_km"] <= radius_km]
    merged.sort(key=lambda p: p["distance_km"])
    return merged[:limit]
