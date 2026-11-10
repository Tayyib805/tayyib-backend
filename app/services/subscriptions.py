"""Subscription state from RevenueCat.

RevenueCat tells us when a subscription starts, renews, changes or ends
(POST /webhooks/revenuecat). The user's tier and expiry live on their
profile; the client can never change them (database trigger).

Two ways to work out the tier, in order of preference:
1. Ask RevenueCat for the subscriber's current entitlements (needs
   REVENUECAT_API_KEY). This is the source of truth and handles every case.
2. Otherwise, read the webhook event itself (rules in decide()).

The pure functions here have no I/O so the rules can be unit tested.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

TIER_RANK = {"free": 0, "standard": 1, "pro": 2}
# After expiry we wait this long before treating someone as free, in case a
# renewal webhook is late. Stores already apply their own billing grace.
EXPIRY_GRACE = timedelta(days=2)

GRANT_EVENTS = {
    "INITIAL_PURCHASE",
    "RENEWAL",
    "UNCANCELLATION",
    "NON_RENEWING_PURCHASE",
    "SUBSCRIPTION_EXTENDED",
    "TEMPORARY_ENTITLEMENT_GRANT",
    "PRODUCT_CHANGE",
}
# Nothing changes until the period actually ends (an EXPIRATION follows).
NO_CHANGE_EVENTS = {"CANCELLATION", "BILLING_ISSUE", "SUBSCRIPTION_PAUSED", "TEST", "INVOICE_ISSUANCE"}


def _ms(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc) if value else None
    except (TypeError, ValueError, OverflowError):
        return None


def parse_time(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def tier_for_entitlements(entitlements: list[str] | None, standard_id: str, pro_id: str) -> str:
    """The best tier among entitlement ids; unknown ids count for nothing."""

    ids = {str(e) for e in (entitlements or [])}
    if pro_id in ids:
        return "pro"
    if standard_id in ids:
        return "standard"
    return "free"


def effective_tier(profile: dict, now: datetime | None = None) -> str:
    """What the person is entitled to right now (an expired paid tier counts as free)."""

    tier = profile.get("subscription_tier") or "free"
    if tier == "free":
        return "free"
    expires = parse_time(profile.get("subscription_expires_at"))
    if expires is not None and expires + EXPIRY_GRACE < (now or datetime.now(timezone.utc)):
        return "free"
    return tier if tier in TIER_RANK else "free"


def decide(event: dict, current: dict, standard_id: str, pro_id: str, now: datetime | None = None) -> dict | None:
    """What a webhook event changes on the profile, or None for no change.

    `current` is the profile's {subscription_tier, subscription_expires_at}.
    Returns {"subscription_tier", "subscription_expires_at", "subscription_store"}.
    """

    now = now or datetime.now(timezone.utc)
    kind = str(event.get("type") or "").upper()
    expires = _ms(event.get("expiration_at_ms"))
    store = event.get("store")
    current_tier = current.get("subscription_tier") or "free"
    current_expires = parse_time(current.get("subscription_expires_at"))
    entitlements = event.get("entitlement_ids") or ([event["entitlement_id"]] if event.get("entitlement_id") else [])

    if kind in GRANT_EVENTS:
        tier = tier_for_entitlements(entitlements, standard_id, pro_id)
        if tier == "free":
            return None  # a product we don't map to a tier
        if expires is not None and expires <= now:
            return None  # an old event arriving late
        if kind == "PRODUCT_CHANGE" and TIER_RANK[tier] < TIER_RANK.get(current_tier, 0):
            return None  # a downgrade takes effect at the next renewal, which sends its own event
        if current_expires and expires and expires < current_expires and TIER_RANK[tier] <= TIER_RANK.get(current_tier, 0):
            return None  # out of order: we already know about a later period
        return {"subscription_tier": tier, "subscription_expires_at": expires.isoformat() if expires else None, "subscription_store": store}

    if kind == "EXPIRATION" or (kind == "CANCELLATION" and expires is not None and expires <= now):
        # Ends now (EXPIRATION), or was refunded (a cancellation whose period is already over).
        if current_tier == "free":
            return None
        if current_expires and expires and current_expires > expires + timedelta(minutes=5):
            return None  # we already recorded a later period (renewed elsewhere, or events out of order)
        return {"subscription_tier": "free", "subscription_expires_at": expires.isoformat() if expires else None, "subscription_store": store}

    return None  # NO_CHANGE_EVENTS and anything new RevenueCat adds


def tier_from_subscriber(subscriber: dict, standard_id: str, pro_id: str, now: datetime | None = None) -> dict:
    """Profile fields from RevenueCat's GET /v1/subscribers/{id} answer."""

    now = now or datetime.now(timezone.utc)
    best, best_expiry, lifetime = "free", None, False
    for ent_id, ent in ((subscriber or {}).get("entitlements") or {}).items():
        tier = tier_for_entitlements([ent_id], standard_id, pro_id)
        if tier == "free":
            continue
        expires = parse_time(ent.get("grace_period_expires_date")) or parse_time(ent.get("expires_date"))
        if expires is not None and expires <= now:
            continue
        if TIER_RANK[tier] > TIER_RANK[best] or (tier == best and not lifetime and (expires is None or (best_expiry and expires > best_expiry))):
            best, best_expiry, lifetime = tier, expires, expires is None
    return {
        "subscription_tier": best,
        "subscription_expires_at": best_expiry.isoformat() if best_expiry else None,
    }


async def fetch_subscriber(app_user_id: str, api_key: str) -> dict | None:
    """RevenueCat's current view of one subscriber, or None if it can't be reached."""

    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"https://api.revenuecat.com/v1/subscribers/{app_user_id}",
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            )
        if response.status_code != 200:
            return None
        return (response.json() or {}).get("subscriber")
    except Exception:
        return None
