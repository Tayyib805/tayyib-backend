"""Authenticated user profile endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from supabase import Client

from app.config import get_settings
from app.services.article_text import clean_display_name
from app.services.subscriptions import effective_tier
from app.database import get_current_user, get_supabase
from app.models.user import UserProfileUpdate
from app.ratelimit import rate_limit
from app.services.push import COMPANIONS

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
            .insert({"id": user["id"]})
            .execute()
        )
        return _with_subscription(supabase, created.data[0] if created.data else {"id": user["id"]})
    return _with_subscription(supabase, response.data[0])


def _with_subscription(supabase: Client, profile: dict) -> dict:
    """Add what the person is entitled to right now; tidy up a subscription that ran out."""

    tier = effective_tier(profile)
    if tier == "free" and (profile.get("subscription_tier") or "free") != "free":
        # The paid period ended and no renewal arrived: record it.
        try:
            supabase.table("user_profiles").update({"subscription_tier": "free"}).eq("id", profile["id"]).execute()
        except Exception:
            pass
        profile["subscription_tier"] = "free"
    profile["subscription"] = {
        "tier": tier,
        "active": tier != "free",
        "expires_at": profile.get("subscription_expires_at") if tier != "free" else None,
        "store": profile.get("subscription_store") if tier != "free" else None,
    }
    return profile


@router.post("/me/subscription/refresh", dependencies=[Depends(rate_limit("subscription_refresh", 10, 3600))])
async def refresh_subscription(
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Ask RevenueCat for this account's current subscription (the app calls this right after a purchase)."""

    from app.routers.platform import apply_subscription

    if get_settings().REVENUECAT_API_KEY:
        await apply_subscription(supabase, user["id"])
    rows = supabase.table("user_profiles").select("*").eq("id", user["id"]).limit(1).execute().data
    return _with_subscription(supabase, rows[0] if rows else {"id": user["id"]})["subscription"]


@router.put("/me")
@router.patch("/me")
async def update_me(
    payload: UserProfileUpdate,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Update the authenticated user's profile preferences (PUT or PATCH)."""

    updates = payload.model_dump(exclude_none=True)
    if "display_name" in updates:
        raw = updates.pop("display_name").strip()
        if not raw:
            updates.update({"display_name": None, "name_chosen": False})
        else:
            cleaned = clean_display_name(raw)
            local_part = (user.get("email") or "").split("@")[0].lower()
            if not cleaned or (local_part and cleaned.lower() == local_part):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Names can be 2–40 characters, without emails, links or your email username.",
                )
            updates.update({"display_name": cleaned, "name_chosen": True})
    madhab = updates.pop("madhab", None)
    if madhab and "madhab_preference" not in updates:
        updates["madhab_preference"] = madhab
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No profile fields provided.",
        )

    companion = updates.get("active_companion")
    if companion:
        needed = next((pts for pts, cid, _ in COMPANIONS if cid == companion), None)
        if needed is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown companion.")
        if needed > 0:
            rows = supabase.table("user_profiles").select("points").eq("id", user["id"]).limit(1).execute().data
            if int((rows[0].get("points") if rows else 0) or 0) < needed:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="That companion isn't unlocked yet.")

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


@router.delete("/me", dependencies=[Depends(rate_limit("delete_account", 5, 3600))])
async def delete_me(
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Delete the signed-in account and everything personal tied to it.

    Profile, scan history, favourites, votes, phone notifications and
    reviews go with the account (database cascades). Contributions others
    rely on (label readings, challenges, suggested places) stay, without
    the name. Required by the App Store for apps with sign-up.
    """

    try:
        supabase.auth.admin.delete_user(user["id"])
    except Exception as exc:
        logging.getLogger("tayyib.users").error("Account deletion failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="We couldn't delete your account just now. Please try again, or email verify@tayyib.app.",
        ) from exc
    return {"deleted": True}
