"""Scan log endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client

from app.database import get_current_user, get_supabase
from app.models.scan import ScanCreate

router = APIRouter()


@router.post("")
async def create_scan(
    payload: ScanCreate,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Create a scan log row for the authenticated user."""

    row = payload.model_dump()
    row["user_id"] = user["id"]
    response = supabase.table("scan_logs").insert(row).execute()
    if not response.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create scan log.",
        )
    return response.data[0]
