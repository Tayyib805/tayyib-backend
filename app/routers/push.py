"""Phone notification sign-up (web push)."""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from supabase import Client

from app.config import get_settings
from app.database import get_current_user, get_supabase
from app.ratelimit import rate_limit
from app.services.push import configured, notify

router = APIRouter()


class Keys(BaseModel):
    p256dh: str = Field(min_length=20, max_length=200)
    auth: str = Field(min_length=8, max_length=100)


class Subscription(BaseModel):
    endpoint: str = Field(min_length=20, max_length=1000, pattern=r"^https://")
    keys: Keys
    user_agent: str | None = Field(default=None, max_length=300)


class Unsubscribe(BaseModel):
    endpoint: str = Field(min_length=20, max_length=1000)


@router.get("/public-key")
async def public_key() -> dict:
    """The key browsers need to subscribe; empty when notifications aren't set up."""

    return {"key": get_settings().VAPID_PUBLIC_KEY if configured() else ""}


@router.post("/subscribe", dependencies=[Depends(rate_limit("push_subscribe", 20, 3600))])
async def subscribe(
    payload: Subscription,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Save this device's subscription for the signed-in user."""

    if not configured():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Notifications aren't set up yet.")
    supabase.table("push_subscriptions").upsert(
        {
            "user_id": user["id"],
            "endpoint": payload.endpoint,
            "p256dh": payload.keys.p256dh,
            "auth": payload.keys.auth,
            "user_agent": payload.user_agent,
        },
        on_conflict="endpoint",
    ).execute()
    return {"subscribed": True}


@router.delete("/subscribe")
async def unsubscribe(
    payload: Unsubscribe,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Stop notifications to this device."""

    supabase.table("push_subscriptions").delete().eq("endpoint", payload.endpoint).eq("user_id", user["id"]).execute()
    return {"subscribed": False}


@router.post("/test", dependencies=[Depends(rate_limit("push_test", 5, 3600))])
async def test(user: dict = Depends(get_current_user), supabase: Client = Depends(get_supabase)) -> dict:
    """Send yourself a sample notification."""

    notify(supabase, [user["id"]], "Peekaboo!", "{companion} here. This is how I'll let you know when something changes.", "/", "test")
    return {"sent": True}
