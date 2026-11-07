"""Scan log endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client

from app.database import get_current_user, get_supabase
from app.models.scan import ScanCreate
from app.services.points import (
    DAILY_SCAN_AWARDS,
    SCAN_NEW_PRODUCT,
    award_points,
    first_scan_of_product,
    under_daily_cap,
)

router = APIRouter()

VERDICTS = {"halal", "haram", "uncertain", "unknown"}


@router.post("")
async def create_scan(
    payload: ScanCreate,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Create a scan log row for the authenticated user."""

    if not payload.product_id and not payload.barcode:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A product_id or barcode is required.",
        )

    verdict = (payload.result_shown or payload.verdict or "").strip().lower() or None
    if verdict and verdict not in VERDICTS:
        verdict = "uncertain"

    row = {
        "user_id": user["id"],
        "product_id": payload.product_id,
        "barcode": payload.barcode,
        "scan_method": payload.scan_method or "barcode",
        "country_code": (payload.country_code or "US").strip().upper()[:2],
        "result_shown": verdict,
        "tier_at_time_of_scan": payload.tier_at_time_of_scan,
    }
    earns = bool(payload.product_id) and first_scan_of_product(supabase, user["id"], payload.product_id) and under_daily_cap(
        supabase, "scan_logs", user["id"], DAILY_SCAN_AWARDS, "with_product"
    )
    response = supabase.table("scan_logs").insert(row).execute()
    if not response.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create scan log.",
        )
    if earns:
        award_points(supabase, user["id"], SCAN_NEW_PRODUCT)
    return {**response.data[0], "points_awarded": SCAN_NEW_PRODUCT if earns else 0}
