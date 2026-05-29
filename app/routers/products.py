"""Product lookup, scan, and voting endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client

from app.database import get_current_user, get_supabase
from app.services.claude_analysis import analyze_ingredients
from app.services.openfoodfacts import lookup_barcode

router = APIRouter()


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


@router.get("/scan/{barcode}")
async def scan_product(barcode: str, supabase: Client = Depends(get_supabase)) -> dict:
    """Scan a barcode, retrieve or create its product record, and return analysis."""

    existing = _fetch_product_with_ingredients(supabase, "barcode", barcode)
    if existing:
        new_count = int(existing.get("scan_count") or 0) + 1
        supabase.table("products").update({"scan_count": new_count}).eq(
            "id", existing["id"]
        ).execute()
        existing["scan_count"] = new_count
        return existing

    off_product = await lookup_barcode(barcode)
    if not off_product.get("found"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")

    known = supabase.table("ingredients").select("name,e_number,default_status").execute()
    analysis = await analyze_ingredients(
        off_product.get("ingredients_text", ""),
        known.data or [],
    )

    created = (
        supabase.table("products")
        .insert(
            {
                "barcode": barcode,
                "name": off_product["name"],
                "brand": off_product.get("brand"),
                "image_url": off_product.get("image_url"),
                "verification_tier": 3,
                "scan_count": 1,
                "overall_status": analysis.get("overall_status", "uncertain"),
                "confidence_level": analysis.get("confidence", "low"),
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
    product["analysis"] = analysis
    product["ingredients_text"] = off_product.get("ingredients_text", "")
    return product


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
