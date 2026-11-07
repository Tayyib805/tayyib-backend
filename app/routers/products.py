"""Product lookup, scan, label enrichment, and voting endpoints."""

import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from supabase import Client

from app.database import get_current_user, get_supabase
from app.services.claude_analysis import analyze_ingredients, extract_ingredients_from_image
from app.ratelimit import rate_limit
from app.services.alternatives import build_alternatives, category_chain, category_label
from app.services.openfoodfacts import lookup_barcode, search_halal_labeled
from app.services.trust import vote_counts, why_not_counted
from app.services.points import CONFIRM_RESULT, DAILY_LABEL_AWARDS, LABEL_PHOTO, award_points, under_daily_cap

router = APIRouter()

BARCODE_PATTERN = re.compile(r"^[A-Za-z0-9-]{4,32}$")
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
# Claude accepts images up to 5 MB; base64 inflates size by about 4/3.
MAX_IMAGE_BASE64_CHARS = 7_000_000


class LabelScanRequest(BaseModel):
    """Payload for scanning an ingredients label image."""

    image_base64: str
    media_type: str = "image/jpeg"
    barcode: str | None = None
    product_name: str | None = None
    brand: str | None = None
    country_code: str = "US"


def _fetch_product_with_ingredients(supabase: Client, column: str, value: str) -> dict | None:
    """Fetch a product and its linked ingredients by a product column."""

    response = (
        supabase.table("products")
        .select("*, product_ingredients(*, ingredients(*))")
        .eq(column, value)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def _normalize_country_code(country_code: str | None) -> str:
    """Normalize a country code for formulation matching."""

    return (country_code or "US").strip().upper() or "US"


def _fetch_latest_formulation(
    supabase: Client,
    product_id: str,
    country_code: str,
) -> dict | None:
    """Fetch the latest formulation for a product/country pair."""

    response = (
        supabase.table("product_formulations")
        .select("*")
        .eq("product_id", product_id)
        .eq("country_code", country_code)
        .order("effective_from", desc=True)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def _fetch_product_by_barcode_country(
    supabase: Client,
    barcode: str,
    country_code: str,
) -> dict | None:
    """Fetch a product by barcode and attach its country-specific formulation."""

    product = _fetch_product_with_ingredients(supabase, "barcode", barcode)
    if not product:
        return None

    product["product_formulation"] = _fetch_latest_formulation(
        supabase,
        product["id"],
        country_code,
    )
    product["country_code"] = country_code
    return product


def _fetch_product_by_name_brand(
    supabase: Client,
    product_name: str,
    brand: str | None,
) -> dict | None:
    """Fetch a product by exact name and optional brand."""

    query = supabase.table("products").select("*, product_ingredients(*, ingredients(*))").eq(
        "name", product_name
    )
    if brand:
        query = query.eq("brand", brand)
    response = query.limit(1).execute()
    return response.data[0] if response.data else None


def _fetch_latest_analysis_flagged_ingredients(
    supabase: Client,
    product_id: str,
) -> list[dict]:
    """Fetch flagged ingredients from the latest stored analysis run."""

    response = (
        supabase.table("analysis_runs")
        .select("result")
        .eq("product_id", product_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not response.data:
        return []

    result = response.data[0].get("result") or {}
    if not isinstance(result, dict):
        return []
    flagged = result.get("flagged_ingredients") or []
    return flagged if isinstance(flagged, list) else []


def _fetch_product_ingredient_flags(
    supabase: Client,
    product_id: str,
) -> list[dict]:
    """Fetch flagged ingredients from product_ingredients links."""

    response = (
        supabase.table("product_ingredients")
        .select("flag_reason,notes,ingredients(name,e_number,default_status)")
        .eq("product_id", product_id)
        .eq("is_flagged", True)
        .execute()
    )

    flagged = []
    for link in response.data or []:
        ingredient = link.get("ingredients") or {}
        name = ingredient.get("name")
        if not name:
            continue
        flagged.append(
            {
                "name": name,
                "status": ingredient.get("default_status") or "uncertain",
                "reason": link.get("flag_reason") or link.get("notes"),
                "e_number": ingredient.get("e_number"),
            }
        )
    return flagged


def _format_flagged_ingredient(flagged: dict) -> dict:
    """Format a flagged ingredient for scan responses."""

    return {
        "name": flagged.get("name"),
        "status": flagged.get("status") or "uncertain",
        "reason": flagged.get("reason"),
        "e_number": flagged.get("e_number"),
    }


def _build_scan_response(
    supabase: Client,
    product: dict,
    country_code: str,
) -> dict:
    """Build the public scan response payload."""

    flagged_ingredients = _fetch_latest_analysis_flagged_ingredients(supabase, product["id"])
    if not flagged_ingredients:
        flagged_ingredients = _fetch_product_ingredient_flags(supabase, product["id"])

    return {
        "id": product["id"],
        "name": product["name"],
        "brand": product.get("brand"),
        "overall_status": product.get("overall_status") or "uncertain",
        "confidence_level": product.get("confidence_level") or "low",
        "flagged_ingredients": [
            _format_flagged_ingredient(flagged)
            for flagged in flagged_ingredients
            if flagged.get("name")
        ],
        "tier": product.get("verification_tier"),
        "scan_count": product.get("scan_count") or 0,
        "country_code": country_code,
        "barcode": product.get("barcode"),
        "image_url": product.get("image_url"),
        "category": category_label(product.get("primary_category")),
    }


def _has_ingredients(product: dict | None) -> bool:
    """Return whether a product has linked ingredients."""

    return bool(product and product.get("product_ingredients"))


def _has_country_formulation(product: dict | None) -> bool:
    """Return whether the product has an ingredients formulation for the country."""

    formulation = product.get("product_formulation") if product else None
    return bool(formulation and formulation.get("ingredients_text"))


def _increment_scan_count(
    supabase: Client,
    product: dict,
    country_code: str | None = None,
) -> dict:
    """Increment scan_count for a product and return the updated product object."""

    new_count = int(product.get("scan_count") or 0) + 1
    supabase.table("products").update({"scan_count": new_count}).eq("id", product["id"]).execute()
    product["scan_count"] = new_count
    if country_code:
        product["country_code"] = country_code
    return product


def _save_product_formulation(
    supabase: Client,
    product_id: str,
    ingredients_text: str,
    country_code: str,
    source_url: str | None = None,
    data_source: str = "open_food_facts",
) -> dict | None:
    """Save a country-specific product formulation."""

    if not ingredients_text:
        return None

    existing = _fetch_latest_formulation(supabase, product_id, country_code)
    payload = {
        "product_id": product_id,
        "ingredients_text": ingredients_text,
        "source_url": source_url,
        "country_code": country_code,
        "data_source": data_source,
    }
    if existing:
        updated = (
            supabase.table("product_formulations")
            .update({key: value for key, value in payload.items() if value is not None})
            .eq("id", existing["id"])
            .execute()
        )
        return updated.data[0] if updated.data else {**existing, **payload}

    created = (
        supabase.table("product_formulations")
        .insert({key: value for key, value in payload.items() if value is not None})
        .execute()
    )
    return created.data[0] if created.data else None


def _ingredient_payload(flagged: dict) -> dict:
    """Build an ingredient row from a flagged ingredient result."""

    return {
        "name": flagged.get("name") or "Unknown ingredient",
        "e_number": flagged.get("e_number"),
        "default_status": flagged.get("status") or "uncertain",
        "has_variants": (flagged.get("status") or "uncertain") == "uncertain",
        "ruling_notes": flagged.get("reason"),
        "education_card_content": flagged.get("reason"),
    }


def _normalize_confidence(confidence: str | None) -> str:
    """Return a valid confidence value."""

    normalized = (confidence or "low").lower()
    return normalized if normalized in {"high", "medium", "low"} else "low"


def _find_ingredient_id(supabase: Client, flagged: dict) -> str | None:
    """Find an ingredient by name or e_number."""

    name = str(flagged.get("name") or "").strip()
    e_number = str(flagged.get("e_number") or "").strip()

    if name:
        ingredient = (
            supabase.table("ingredients")
            .select("id")
            .eq("name", name)
            .limit(1)
            .execute()
        )
        if ingredient.data:
            return ingredient.data[0]["id"]

    if e_number:
        ingredient = (
            supabase.table("ingredients")
            .select("id")
            .eq("e_number", e_number)
            .limit(1)
            .execute()
        )
        if ingredient.data:
            return ingredient.data[0]["id"]

    return None


def _get_or_create_ingredient_id(supabase: Client, flagged: dict) -> str | None:
    """Return an ingredient id, creating the ingredient if needed."""

    ingredient_id = _find_ingredient_id(supabase, flagged)
    if ingredient_id:
        return ingredient_id

    created = supabase.table("ingredients").insert(_ingredient_payload(flagged)).execute()
    if not created.data:
        return None
    return created.data[0]["id"]


def _product_ingredient_payload(
    product_id: str,
    ingredient_id: str,
    flagged: dict,
    confidence: str,
) -> dict:
    """Build a product_ingredients link payload from Claude analysis."""

    flagged_status = flagged.get("status") or "uncertain"
    reason = flagged.get("reason")
    return {
        "product_id": product_id,
        "ingredient_id": ingredient_id,
        "variant_risk": flagged_status == "uncertain",
        "notes": reason,
        "is_flagged": True,
        "flag_reason": reason,
        "confidence": confidence,
    }


def _save_flagged_ingredients(
    supabase: Client,
    product_id: str,
    flagged_ingredients: list[dict],
    confidence: str | None,
) -> None:
    """Persist flagged ingredients and link them to the product."""

    normalized_confidence = _normalize_confidence(confidence)
    for flagged in flagged_ingredients:
        name = str(flagged.get("name") or "").strip()
        if not name:
            continue

        ingredient_id = _get_or_create_ingredient_id(supabase, flagged)
        if not ingredient_id:
            continue

        existing_link = (
            supabase.table("product_ingredients")
            .select("id")
            .eq("product_id", product_id)
            .eq("ingredient_id", ingredient_id)
            .limit(1)
            .execute()
        )
        payload = _product_ingredient_payload(
            product_id,
            ingredient_id,
            flagged,
            normalized_confidence,
        )
        if existing_link.data:
            update_payload = {
                key: value
                for key, value in payload.items()
                if key not in {"product_id", "ingredient_id"}
            }
            supabase.table("product_ingredients").update(update_payload).eq(
                "id",
                existing_link.data[0]["id"],
            ).execute()
            continue

        supabase.table("product_ingredients").insert(payload).execute()


async def _analyze_and_enrich_product(
    supabase: Client,
    product: dict | None,
    off_product: dict,
    barcode: str,
    country_code: str,
) -> dict:
    """Analyze Open Food Facts ingredients and save the result to Supabase."""

    known = supabase.table("ingredients").select("name,e_number,default_status").execute()
    ingredients_text = off_product.get("ingredients_text", "")
    analysis = await analyze_ingredients(ingredients_text, known.data or [])
    updates = {
        "name": off_product["name"],
        "brand": off_product.get("brand"),
        "image_url": off_product.get("image_url"),
        "overall_status": analysis.get("overall_status", "uncertain"),
        "confidence_level": analysis.get("confidence", "low"),
        "ingredients_text": ingredients_text,
        "data_source": "open_food_facts",
    }
    if off_product.get("categories"):
        updates["categories"] = off_product["categories"]
        updates["primary_category"] = off_product.get("primary_category")

    if product:
        scan_count = int(product.get("scan_count") or 0) + 1
        updates["scan_count"] = scan_count
        updated = supabase.table("products").update(updates).eq("id", product["id"]).execute()
        saved = updated.data[0] if updated.data else {**product, **updates}
    else:
        created = (
            supabase.table("products")
            .insert(
                {
                    "barcode": barcode,
                    "verification_tier": 3,
                    "scan_count": 1,
                    **updates,
                }
            )
            .execute()
        )
        if not created.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create product.",
            )
        saved = created.data[0]

    formulation = _save_product_formulation(
        supabase,
        saved["id"],
        ingredients_text,
        country_code,
        source_url=off_product.get("website"),
        data_source="open_food_facts",
    )
    _save_flagged_ingredients(
        supabase,
        saved["id"],
        analysis.get("flagged_ingredients") or [],
        analysis.get("confidence"),
    )
    enriched = _fetch_product_with_ingredients(supabase, "id", saved["id"]) or saved
    enriched["country_code"] = country_code
    enriched["product_formulation"] = formulation
    return _build_scan_response(supabase, enriched, country_code)


@router.get(
    "/scan/{barcode}",
    dependencies=[
        Depends(rate_limit("scan_minute", 30, 60)),
        Depends(rate_limit("scan_day", 500, 86_400)),
    ],
)
async def scan_product(
    barcode: str,
    country_code: str = "US",
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Scan a barcode, preferring Tayyib data before enriching from Open Food Facts."""

    if not BARCODE_PATTERN.match(barcode):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid barcode.")
    normalized_country = _normalize_country_code(country_code)
    existing = _fetch_product_by_barcode_country(supabase, barcode, normalized_country)
    if existing and _has_ingredients(existing) and _has_country_formulation(existing):
        product = _increment_scan_count(supabase, existing, normalized_country)
        return _build_scan_response(supabase, product, normalized_country)

    off_product = await lookup_barcode(barcode, normalized_country)
    if not off_product.get("found"):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "found": False,
                "barcode": barcode,
                "country_code": normalized_country,
                "message": "Product not in database. Please scan the ingredients label.",
                "requires_label_scan": True,
            },
        )

    return await _analyze_and_enrich_product(
        supabase,
        existing,
        off_product,
        barcode,
        normalized_country,
    )


@router.post(
    "/scan/label",
    dependencies=[
        Depends(rate_limit("label_minute", 6, 60)),
        Depends(rate_limit("label_day", 60, 86_400)),
    ],
)
async def scan_product_label(
    payload: LabelScanRequest,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Scan an ingredients label image and permanently enrich the product database.

    Requires sign-in, because label scans write to the shared community database.
    """

    if payload.media_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported image type. Use JPEG, PNG, WebP, or GIF.",
        )
    if len(payload.image_base64) > MAX_IMAGE_BASE64_CHARS:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Image is too large. Please use a photo under 5 MB.",
        )
    if payload.barcode and not BARCODE_PATTERN.match(payload.barcode):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid barcode.")

    extraction = await extract_ingredients_from_image(payload.image_base64, payload.media_type)
    if not extraction.get("found") or not extraction.get("ingredients_text"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No ingredients list visible. Please photograph the ingredients label directly.",
        )

    product_name = payload.product_name or extraction.get("product_name")
    brand = payload.brand or extraction.get("brand")
    if not product_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Product name is required when it cannot be extracted from the label.",
        )

    ingredients_text = extraction["ingredients_text"]
    country_code = _normalize_country_code(payload.country_code)
    known = supabase.table("ingredients").select("name,e_number,default_status").execute()
    analysis = await analyze_ingredients(ingredients_text, known.data or [])

    existing = None
    if payload.barcode:
        existing = _fetch_product_with_ingredients(supabase, "barcode", payload.barcode)
    if not existing:
        existing = _fetch_product_by_name_brand(supabase, product_name, brand)

    product_payload = {
        "name": product_name,
        "brand": brand,
        "barcode": payload.barcode,
        "overall_status": analysis.get("overall_status", "uncertain"),
        "confidence_level": analysis.get("confidence", "low"),
        "verification_tier": 3,
        "data_source": "label_scan",
        "ingredients_text": ingredients_text,
    }

    first_scan = existing is None
    # A label photo must never overwrite a community-reviewed or scholar-verified
    # verdict. For those products the scan is recorded as a new formulation only.
    protected = bool(existing) and int(existing.get("verification_tier") or 3) < 3
    if protected:
        product = existing
    elif existing:
        update_payload = {key: value for key, value in product_payload.items() if value is not None}
        update_payload["scan_count"] = int(existing.get("scan_count") or 0) + 1
        updated = (
            supabase.table("products")
            .update(update_payload)
            .eq("id", existing["id"])
            .execute()
        )
        product = updated.data[0] if updated.data else {**existing, **update_payload}
    else:
        created = (
            supabase.table("products")
            .insert(
                {
                    **{key: value for key, value in product_payload.items() if value is not None},
                    "scan_count": 1,
                }
            )
            .execute()
        )
        if not created.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create product.",
            )
        product = created.data[0]

    if not protected:
        _save_flagged_ingredients(
            supabase,
            product["id"],
            analysis.get("flagged_ingredients") or [],
            analysis.get("confidence"),
        )
    formulation = _save_product_formulation(
        supabase,
        product["id"],
        ingredients_text,
        country_code,
        data_source="label_scan",
    )
    earns = under_daily_cap(supabase, "label_scans", user["id"], DAILY_LABEL_AWARDS)
    supabase.table("label_scans").insert(
        {
            "product_id": product["id"],
            "user_id": user["id"],
            "extracted_ingredients": ingredients_text,
            "extraction_confidence": extraction.get("confidence") or "low",
            "country_code": country_code,
        }
    ).execute()
    if earns:
        award_points(supabase, user["id"], LABEL_PHOTO)

    enriched = _fetch_product_with_ingredients(supabase, "id", product["id"]) or product
    enriched["country_code"] = country_code
    enriched["product_formulation"] = formulation
    response = _build_scan_response(supabase, enriched, country_code)
    response["points_awarded"] = LABEL_PHOTO if earns else 0
    return response


_stats_cache: dict = {"at": 0.0, "data": None}


@router.get("/stats")
async def product_stats(supabase: Client = Depends(get_supabase)) -> dict:
    """Return live database totals for the home screen (cached for five minutes)."""

    now = time.monotonic()
    if _stats_cache["data"] and now - _stats_cache["at"] < 300:
        return _stats_cache["data"]

    def count(query) -> int:
        return int(query.limit(1).execute().count or 0)

    data = {
        "products": count(supabase.table("products").select("id", count="exact")),
        "halal": count(supabase.table("products").select("id", count="exact").eq("overall_status", "halal")),
        "reviewed": count(supabase.table("products").select("id", count="exact").lt("verification_tier", 3)),
    }
    _stats_cache.update(at=now, data=data)
    return data


@router.get(
    "/{product_id}/alternatives",
    dependencies=[Depends(rate_limit("alternatives_minute", 30, 60))],
)
async def get_halal_alternatives(
    product_id: str,
    country_code: str = "US",
    limit: int = Query(default=6, ge=1, le=12),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Suggest halal products in the same category, sold in the user's country.

    Our own halal verdicts come first. Open Food Facts products with a halal
    certification label fill the rest and are marked as label claims.
    """

    found = (
        supabase.table("products")
        .select("id,barcode,categories,primary_category")
        .eq("id", product_id)
        .limit(1)
        .execute()
    )
    if not found.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")
    product = found.data[0]
    country = _normalize_country_code(country_code)

    categories = product.get("categories") or []
    primary = product.get("primary_category")
    if not categories and product.get("barcode"):
        # Products saved before categories existed: fill them in once.
        off_product = await lookup_barcode(product["barcode"], country)
        if off_product.get("found") and off_product.get("categories"):
            categories = off_product["categories"]
            primary = off_product.get("primary_category")
            supabase.table("products").update(
                {"categories": categories, "primary_category": primary}
            ).eq("id", product_id).execute()

    chain = category_chain(categories, primary)
    response = {
        "product_id": product_id,
        "country_code": country,
        "category": category_label(chain[0]) if chain else None,
        "items": [],
        "label_search": "skipped",
    }
    if not chain:
        return response

    array_literal = "{" + ",".join(f'"{tag}"' for tag in chain) + "}"
    tayyib_rows = (
        supabase.table("products")
        .select("id,barcode,name,brand,image_url,verification_tier,scan_count,categories")
        .eq("overall_status", "halal")
        .neq("id", product_id)
        .filter("categories", "ov", array_literal)
        .limit(40)
        .execute()
    ).data or []
    if tayyib_rows:
        # A halal verdict only counts where we hold that country's formulation.
        with_formulation = (
            supabase.table("product_formulations")
            .select("product_id")
            .eq("country_code", country)
            .in_("product_id", [row["id"] for row in tayyib_rows])
            .execute()
        ).data or []
        allowed = {row["product_id"] for row in with_formulation}
        tayyib_rows = [row for row in tayyib_rows if row["id"] in allowed]

    label_rows: list[dict] = []
    label_search = "ok"
    if len(tayyib_rows) < limit:
        for tag in chain[:2]:
            results = await search_halal_labeled(tag, country, limit=12)
            if results is None:
                label_search = "unavailable"
                break
            label_rows.extend(results)
            if len(tayyib_rows) + len(label_rows) >= limit * 2:
                break

    known_status: dict[str, str] = {}
    label_barcodes = list({row["barcode"] for row in label_rows})[:100]
    if label_barcodes:
        known = (
            supabase.table("products")
            .select("barcode,overall_status")
            .in_("barcode", label_barcodes)
            .execute()
        ).data or []
        known_status = {row["barcode"]: row.get("overall_status") or "uncertain" for row in known}

    response["items"] = build_alternatives(
        scanned_barcode=product.get("barcode"),
        chain=chain,
        tayyib_rows=tayyib_rows,
        label_rows=label_rows,
        known_status_by_barcode=known_status,
        limit=limit,
    )
    response["label_search"] = label_search
    return response


@router.get("/{product_id}")
async def get_product(product_id: str, supabase: Client = Depends(get_supabase)) -> dict:
    """Return a product by ID with linked ingredients."""

    product = _fetch_product_with_ingredients(supabase, "id", product_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")
    return product


@router.post("/{product_id}/vote")
async def vote_to_elevate_product(
    product_id: str,
    reason: str = "regular_user",
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Add a vote to elevate product verification tier and return the updated product."""

    product = _fetch_product_with_ingredients(supabase, "id", product_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")

    try:
        supabase.table("tier_votes").insert(
            {"product_id": product_id, "user_id": user["id"], "reason": reason}
        ).execute()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User has already voted for this product.",
        ) from exc

    award_points(supabase, user["id"], CONFIRM_RESULT)

    if not vote_counts(supabase, user):
        # Recorded (so it can't be repeated) but doesn't move the tier yet.
        return {**product, "points_awarded": CONFIRM_RESULT, "counted": False, "message": why_not_counted()}

    vote_count = int(product.get("vote_count") or 0) + 1
    verification_tier = int(product.get("verification_tier") or 3)
    votes_needed = int(product.get("votes_needed_to_elevate") or 25)
    # Community votes can raise a product to Tier 2 (Community Reviewed) at most.
    # Tier 1 (Scholar Verified) is only ever set by a scholar or admin.
    if vote_count >= votes_needed and verification_tier > 2:
        verification_tier = 2

    updated = (
        supabase.table("products")
        .update({"vote_count": vote_count, "verification_tier": verification_tier})
        .eq("id", product_id)
        .execute()
    )
    result = dict(updated.data[0]) if updated.data else {"id": product_id}
    result["points_awarded"] = CONFIRM_RESULT
    result["counted"] = True
    return result
