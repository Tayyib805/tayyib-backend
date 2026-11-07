"""Pick halal alternatives for a scanned product.

Pure functions only (no I/O), so the ranking rules can be unit tested.

Two sources, never mixed up in the response:
- "tayyib": products in our own database whose verdict is halal for the
  user's country. Their tier says how far that verdict has been reviewed.
- "label": products Open Food Facts lists in the same category with a halal
  certification label. That is the manufacturer's claim, not our verdict, and
  is dropped whenever our own analysis of the same barcode disagrees.
"""

from __future__ import annotations

# Category tags too broad to mean "a similar product".
UMBRELLA_CATEGORIES = {
    "en:foods",
    "en:food",
    "en:groceries",
    "en:plant-based-foods-and-beverages",
    "en:plant-based-foods",
    "en:beverages-and-beverages-preparations",
    "en:snacks",
    "en:sweet-snacks",
    "en:salty-snacks",
    "en:meals",
    "en:dairies",
    "en:fermented-foods",
    "en:fermented-milk-products",
    "en:frozen-foods",
    "en:canned-foods",
    "en:desserts",
}


def category_label(tag: str | None) -> str | None:
    """Turn a tag like "en:breakfast-cereals" into "Breakfast cereals"."""

    if not tag:
        return None
    name = tag.split(":", 1)[-1].replace("-", " ").strip()
    return name[:1].upper() + name[1:] if name else None


def category_chain(categories: list[str] | None, primary: str | None, max_levels: int = 3) -> list[str]:
    """Return categories to search, most specific first, skipping umbrella tags.

    `categories` is ordered general to specific, as Open Food Facts stores it.
    """

    ordered = [tag for tag in (categories or []) if isinstance(tag, str) and tag]
    if primary and primary in ordered:
        ordered = ordered[: ordered.index(primary) + 1]
    elif primary:
        ordered = [*ordered, primary]

    chain: list[str] = []
    for tag in reversed(ordered):
        if tag in UMBRELLA_CATEGORIES or tag in chain:
            continue
        chain.append(tag)
        if len(chain) >= max_levels:
            break
    return chain


def _specificity(candidate_categories: list[str] | None, chain: list[str]) -> int:
    """Index of the most specific shared category (0 is best), or len(chain)."""

    owned = set(candidate_categories or [])
    for index, tag in enumerate(chain):
        if tag in owned:
            return index
    return len(chain)


def rank_tayyib_candidates(rows: list[dict], chain: list[str]) -> list[dict]:
    """Order our halal products: closest category, then most reviewed, then most scanned."""

    return sorted(
        rows,
        key=lambda row: (
            _specificity(row.get("categories"), chain),
            int(row.get("verification_tier") or 3),
            -int(row.get("scan_count") or 0),
        ),
    )


TIER_NOTES = {
    1: "Scholar verified",
    2: "Community reviewed",
    3: "Initial screening: halal",
}


def build_alternatives(
    *,
    scanned_barcode: str | None,
    chain: list[str],
    tayyib_rows: list[dict],
    label_rows: list[dict],
    known_status_by_barcode: dict[str, str],
    limit: int = 6,
) -> list[dict]:
    """Merge both sources into one list, ours first, without duplicates.

    `known_status_by_barcode` holds our verdict for any label-source barcode
    we already have; a halal-labelled product we found haram or uncertain is
    never suggested.
    """

    items: list[dict] = []
    seen: set[str] = set()
    if scanned_barcode:
        seen.add(scanned_barcode)

    for row in rank_tayyib_candidates(tayyib_rows, chain):
        barcode = row.get("barcode")
        key = barcode or f"id:{row.get('id')}"
        if key in seen:
            continue
        seen.add(key)
        tier = int(row.get("verification_tier") or 3)
        items.append(
            {
                "source": "tayyib",
                "product_id": row.get("id"),
                "barcode": barcode,
                "name": row.get("name"),
                "brand": row.get("brand"),
                "image_url": row.get("image_url"),
                "tier": tier,
                "note": TIER_NOTES.get(tier, TIER_NOTES[3]),
            }
        )

    for row in label_rows:
        barcode = row.get("barcode")
        if not barcode or barcode in seen:
            continue
        known = known_status_by_barcode.get(barcode)
        if known is not None and known != "halal":
            continue
        seen.add(barcode)
        items.append(
            {
                "source": "label",
                "product_id": None,
                "barcode": barcode,
                "name": row.get("name"),
                "brand": row.get("brand"),
                "image_url": row.get("image_url"),
                "tier": None,
                "note": (
                    "Halal label on pack · our screening agrees"
                    if known == "halal"
                    else "Halal label on pack · not yet checked by tayyib"
                ),
            }
        )

    return items[:limit]
