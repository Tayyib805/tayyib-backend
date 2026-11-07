"""Authenticated user profile endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from supabase import Client

from app.database import get_current_user, get_supabase
from app.models.user import UserProfileUpdate

router = APIRouter()


@router.get("/me")
async def get_me(
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Return the authenticated user's profile."""

    response = (
        supabase.table("user_profiles")
        .select("*")
        .eq("id", user["id"])
        .limit(1)
        .execute()
    )
    if not response.data:
        created = (
            supabase.table("user_profiles")
            .insert({"id": user["id"], "display_name": user.get("email")})
            .execute()
        )
        return created.data[0] if created.data else {"id": user["id"]}
    return response.data[0]


@router.put("/me")
@router.patch("/me")
async def update_me(
    payload: UserProfileUpdate,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Update the authenticated user's profile preferences (PUT or PATCH)."""

    updates = payload.model_dump(exclude_none=True)
    madhab = updates.pop("madhab", None)
    if madhab and "madhab_preference" not in updates:
        updates["madhab_preference"] = madhab
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No profile fields provided.",
        )

    # Upsert so the first update also creates the row. Privileged columns are
    # never in `updates`, and a database trigger guards them as well.
    response = (
        supabase.table("user_profiles")
        .upsert({"id": user["id"], **updates}, on_conflict="id")
        .execute()
    )
    return response.data[0] if response.data else {"id": user["id"], **updates}


@router.get("/me/scans")
async def get_my_scans(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Return authenticated user's paginated scan history, newest first."""

    start = (page - 1) * page_size
    end = start + page_size - 1
    response = (
        supabase.table("scan_logs")
        .select("*, products(id,name,brand,barcode,image_url,overall_status,verification_tier)", count="exact")
        .eq("user_id", user["id"])
        .order("scanned_at", desc=True)
        .range(start, end)
        .execute()
    )
    return {
        "page": page,
        "page_size": page_size,
        "total": response.count or 0,
        "items": response.data or [],
    }
