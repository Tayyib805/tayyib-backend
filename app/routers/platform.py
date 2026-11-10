"""Things outside the product itself: app configuration and payment webhooks."""

from __future__ import annotations

import hmac
import logging
import re

from fastapi import APIRouter, Depends, Header, HTTPException, status
from supabase import Client

from app.config import get_settings
from app.database import get_supabase
from app.services import subscriptions as subs

log = logging.getLogger("tayyib.platform")
router = APIRouter()

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@router.get("/config")
async def app_config() -> dict:
    """Settings the app reads at start, so launch switches don't need a new build."""

    s = get_settings()
    return {
        "map": {
            "tile_url": s.MAP_TILE_URL or None,
            "tile_url_dark": s.MAP_TILE_URL_DARK or None,
            "attribution": s.MAP_TILE_ATTRIBUTION or None,
        },
        "sign_in": {"apple": bool(s.APPLE_SIGN_IN_ENABLED)},
        "subscriptions": {
            "open": bool(s.SUBSCRIPTIONS_OPEN),
            "web_key": s.REVENUECAT_PUBLIC_WEB_KEY or None,
            "entitlements": {"standard": s.REVENUECAT_ENTITLEMENT_STANDARD, "pro": s.REVENUECAT_ENTITLEMENT_PRO},
        },
    }


def _user_ids(event: dict) -> list[str]:
    """Our account ids named on an event (we use the Supabase user id as RevenueCat's app user id)."""

    seen: list[str] = []
    for value in [event.get("app_user_id"), event.get("original_app_user_id"), *(event.get("aliases") or [])]:
        value = str(value or "").lower()
        if UUID.match(value) and value not in seen:
            seen.append(value)
    return seen


async def apply_subscription(supabase: Client, user_id: str, event: dict | None = None) -> str | None:
    """Bring one profile's subscription fields up to date. Returns the tier now stored, or None if no profile."""

    s = get_settings()
    rows = (
        supabase.table("user_profiles").select("id,subscription_tier,subscription_expires_at,subscription_store").eq("id", user_id).limit(1).execute().data
    )
    if not rows:
        return None
    current = rows[0]
    change = None
    if s.REVENUECAT_API_KEY:
        subscriber = await subs.fetch_subscriber(user_id, s.REVENUECAT_API_KEY)
        if subscriber is not None:
            change = subs.tier_from_subscriber(subscriber, s.REVENUECAT_ENTITLEMENT_STANDARD, s.REVENUECAT_ENTITLEMENT_PRO)
            if event and event.get("store"):
                change["subscription_store"] = event["store"]
    if change is None and event is not None:
        change = subs.decide(event, current, s.REVENUECAT_ENTITLEMENT_STANDARD, s.REVENUECAT_ENTITLEMENT_PRO)
    if change:
        supabase.table("user_profiles").update(change).eq("id", user_id).execute()
        return change["subscription_tier"]
    return current.get("subscription_tier") or "free"


@router.post("/webhooks/revenuecat")
async def revenuecat_webhook(
    payload: dict,
    authorization: str | None = Header(default=None),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """RevenueCat calls this when a subscription changes.

    Refused unless the Authorization header matches REVENUECAT_WEBHOOK_AUTH
    (set the same value in RevenueCat's webhook settings). Each event is
    applied once; repeats are acknowledged and ignored.
    """

    s = get_settings()
    if not s.REVENUECAT_WEBHOOK_AUTH:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Subscriptions aren't set up yet.")
    if not authorization or not hmac.compare_digest(authorization.encode(), s.REVENUECAT_WEBHOOK_AUTH.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authorised.")

    event = (payload or {}).get("event") or {}
    event_id, kind = str(event.get("id") or ""), str(event.get("type") or "").upper()
    if not event_id or not kind:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a RevenueCat event.")
    if str(event.get("environment") or "").upper() == "SANDBOX" and not s.REVENUECAT_ACCEPT_SANDBOX:
        return {"received": True, "applied": False, "reason": "sandbox events are ignored"}

    if supabase.table("subscription_events").select("id").eq("id", event_id).limit(1).execute().data:
        return {"received": True, "applied": False, "reason": "already processed"}

    # A transfer moves a purchase between accounts: update both sides.
    users = _user_ids(event)
    if kind == "TRANSFER":
        users = [u for u in [*(event.get("transferred_from") or []), *(event.get("transferred_to") or [])] if UUID.match(str(u).lower())]
        users = [str(u).lower() for u in users]

    applied: dict[str, str] = {}
    for user_id in users:
        if kind == "TRANSFER" and not s.REVENUECAT_API_KEY:
            # Without asking RevenueCat we only know who lost it.
            if user_id in [str(u).lower() for u in (event.get("transferred_from") or [])]:
                supabase.table("user_profiles").update({"subscription_tier": "free", "subscription_expires_at": None}).eq("id", user_id).execute()
                applied[user_id] = "free"
            continue
        tier = await apply_subscription(supabase, user_id, event)
        if tier is not None:
            applied[user_id] = tier
            if kind != "TRANSFER":
                break  # the first id that is one of our accounts

    owner = next(iter(applied), None)
    expires = subs._ms(event.get("expiration_at_ms"))
    happened = subs._ms(event.get("event_timestamp_ms"))
    supabase.table("subscription_events").upsert(
        {
            "id": event_id,
            "user_id": owner,
            "type": kind,
            "product_id": event.get("product_id"),
            "entitlements": [str(e) for e in (event.get("entitlement_ids") or [])],
            "store": event.get("store"),
            "environment": event.get("environment"),
            "expires_at": expires.isoformat() if expires else None,
            "event_at": happened.isoformat() if happened else None,
            "applied_tier": applied.get(owner) if owner else None,
        },
        on_conflict="id",
    ).execute()
    if not applied:
        log.warning("RevenueCat %s %s: no matching account", kind, event_id)
    return {"received": True, "applied": bool(applied)}
