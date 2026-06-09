"""Product lookup, scan, label enrichment, and voting endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from supabase import Client

from app.database import get_current_user, get_supabase
from app.services.claude_analysis import analyze_ingredients, extract_ingredients_from_image
from app.services.openfoodfacts import lookup_barcode

router = APIRouter()


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
    enriched["analysis"] = analysis
    enriched["country_code"] = country_code
    enriched["product_formulation"] = formulation
    enriched["source"] = "open_food_facts"
    return enriched


@router.get("/scan/{barcode}")
async def scan_product(
    barcode: str,
    country_code: str = "US",
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Scan a barcode, preferring Tayyib data before enriching from Open Food Facts."""

    normalized_country = _normalize_country_code(country_code)
    existing = _fetch_product_by_barcode_country(supabase, barcode, normalized_country)
    if existing and _has_ingredients(existing) and _has_country_formulation(existing):
        return _increment_scan_count(supabase, existing, normalized_country)

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


@router.post("/scan/label")
async def scan_product_label(
    payload: LabelScanRequest,
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Scan an ingredients label image and permanently enrich the product database."""

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
    if existing:
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
    supabase.table("label_scans").insert(
        {
            "product_id": product["id"],
            "extracted_ingredients": ingredients_text,
            "extraction_confidence": extraction.get("confidence") or "low",
            "country_code": country_code,
        }
    ).execute()

    enriched = _fetch_product_with_ingredients(supabase, "id", product["id"]) or product
    enriched["analysis"] = analysis
    enriched["extraction"] = extraction
    enriched["source"] = "label_scan"
    enriched["first_scan"] = first_scan
    enriched["country_code"] = country_code
    enriched["product_formulation"] = formulation
    return enriched


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

    vote_count = int(product.get("vote_count") or 0) + 1
    verification_tier = int(product.get("verification_tier") or 3)
    votes_needed = int(product.get("votes_needed_to_elevate") or 25)
    if vote_count >= votes_needed and verification_tier > 1:
        verification_tier -= 1

    updated = (
        supabase.table("products")
        .update({"vote_count": vote_count, "verification_tier": verification_tier})
        .eq("id", product_id)
        .execute()
    )
    return updated.data[0] if updated.data else {"id": product_id}
