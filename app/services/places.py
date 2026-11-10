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


# ── Stored places ───────────────────────────────────────────────────────────

KIND_LABELS = {
    **FOOD_AMENITIES,
    "food_truck": "Food truck",
    "bakery": "Bakery",
    "grocery": "Grocery",
    "butcher": "Butcher",
    "prayer_space": "Prayer space",
}


def osm_import_item(element: dict) -> dict | None:
    """One Overpass element as an item for import_places(), or None if it shouldn't be listed."""

    place = classify_osm(element, (0.0, 0.0))
    if not place:
        return None
    tags = element.get("tags") or {}
    cuisine = [c.strip().replace("_", " ") for c in re.split(r"[;,]", tags.get("cuisine") or "") if c.strip()]
    item = {
        "external_id": place["id"].split(":", 1)[1],
        "name": place["name"],
        "lat": place["lat"],
        "lng": place["lng"],
        "address": place["address"],
        "city": tags.get("addr:city"),
        "region": tags.get("addr:state"),
        "phone": place["phone"],
        "website": place["website"],
        "cuisine": [c for c in cuisine if c.lower() not in {"halal", "regional"}][:4],
        "kind": place["kind"],
        "opening_hours": place["opening_hours"],
        "halal": True,
        "data": {"diet_halal": tags.get("diet:halal"), "certifier": place["certifier"]},
    }
    # "only" means the whole menu is halal; "yes" only says halal food is served.
    if place["halal_level"] == "fully_halal":
        item["halal_menu"] = "full"
    return item


def present_place(row: dict) -> dict:
    """A stored place as the app shows it. Unknown stays unknown; nothing is assumed."""

    menu = row.get("halal_menu") or "unknown"
    cuisine = [c[:1].upper() + c[1:] for c in (row.get("cuisine") or []) if c][:3]
    distance = row.get("distance_m")
    return {
        "id": row["id"],
        "source": "tayyib",
        "name": row.get("name"),
        "lat": row.get("lat"),
        "lng": row.get("lng"),
        "distance_km": round(distance / 1000, 2) if distance is not None else None,
        "kind": row.get("kind") or "restaurant",
        "kind_label": KIND_LABELS.get(row.get("kind") or "restaurant", "Restaurant"),
        "cuisine": ", ".join(cuisine) or None,
        "halal_level": {"full": "fully_halal", "partial": "halal_options"}.get(menu, "halal"),
        "certified": row.get("tier") == "certified",
        "certifier": row.get("certifier"),
        "certified_until": row.get("certified_until"),
        "address": ", ".join(x for x in (row.get("address"), row.get("city")) if x) or None,
        "phone": row.get("phone"),
        "website": row.get("website"),
        "opening_hours": row.get("opening_hours"),
        "prayer_space": {"designated": True, "on_request": True, "none": False}.get(row.get("prayer") or "unknown"),
        "avg_rating": None,
        "review_count": 0,
        # Evidence-based labels.
        "tier": row.get("tier") or "listed",
        "confidence": row.get("confidence") or 0,
        "status": row.get("status") or "active",
        "labels": {
            "halal_menu": menu,
            "slaughter": row.get("slaughter") or "unknown",
            "alcohol": row.get("alcohol") or "unknown",
            "pork": row.get("pork") or "unknown",
            "prayer": row.get("prayer") or "unknown",
        },
        "disagreeing_people": row.get("disagreeing_people") or 0,
        "closed_reports": row.get("closed_reports") or 0,
        "last_confirmed_at": row.get("last_confirmed_at"),
    }
