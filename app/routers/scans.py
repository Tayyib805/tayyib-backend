"""Scan log endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client

from app.database import get_current_user, get_supabase
from app.models.scan import ScanCreate

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
    response = supabase.table("scan_logs").insert(row).execute()
    if not response.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create scan log.",
        )
    return response.data[0]
