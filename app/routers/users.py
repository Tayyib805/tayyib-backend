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
async def update_me(
    payload: UserProfileUpdate,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Update authenticated user's profile preferences."""

    updates = payload.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No profile fields provided.",
        )

    response = (
        supabase.table("user_profiles")
        .update(updates)
        .eq("id", user["id"])
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
        .select("*, products(*)")
        .eq("user_id", user["id"])
        .order("scanned_at", desc=True)
        .range(start, end)
        .execute()
    )
    return {"page": page, "page_size": page_size, "items": response.data or []}
