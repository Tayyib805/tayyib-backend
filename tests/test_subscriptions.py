"""Subscription rules: what each RevenueCat event changes, and what counts as active."""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

_spec = importlib.util.spec_from_file_location("subscriptions", Path(__file__).resolve().parents[1] / "app/services/subscriptions.py")
subs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(subs)

NOW = datetime(2027, 2, 10, 12, 0, tzinfo=timezone.utc)
FREE = {"subscription_tier": "free", "subscription_expires_at": None}


def ms(dt):
    return int(dt.timestamp() * 1000)


def event(kind, entitlements=("standard",), expires=NOW + timedelta(days=30), **extra):
    return {"id": "e1", "type": kind, "entitlement_ids": list(entitlements), "expiration_at_ms": ms(expires) if expires else None, "store": "APP_STORE", **extra}


def decide(ev, current=FREE):
    return subs.decide(ev, current, "standard", "pro", NOW)


def test_purchase_and_renewal_grant_the_mapped_tier():
    out = decide(event("INITIAL_PURCHASE"))
    assert out["subscription_tier"] == "standard" and out["subscription_store"] == "APP_STORE"
    assert subs.parse_time(out["subscription_expires_at"]) == NOW + timedelta(days=30)
    assert decide(event("RENEWAL", ("pro", "standard")))["subscription_tier"] == "pro"
    assert decide(event("INITIAL_PURCHASE", ("something_else",))) is None, "unknown products change nothing"
    assert decide({"id": "e", "type": "INITIAL_PURCHASE", "entitlement_id": "pro", "expiration_at_ms": ms(NOW + timedelta(days=5))})["subscription_tier"] == "pro"


def test_cancelling_keeps_access_until_the_period_ends():
    paid = {"subscription_tier": "standard", "subscription_expires_at": (NOW + timedelta(days=10)).isoformat()}
    assert decide(event("CANCELLATION", expires=NOW + timedelta(days=10)), paid) is None
    assert decide(event("BILLING_ISSUE"), paid) is None
    ended = decide(event("EXPIRATION", expires=NOW - timedelta(minutes=1)), {**paid, "subscription_expires_at": (NOW - timedelta(minutes=1)).isoformat()})
    assert ended["subscription_tier"] == "free"
    refunded = decide(event("CANCELLATION", expires=NOW - timedelta(hours=1), cancel_reason="CUSTOMER_SUPPORT"), {**paid, "subscription_expires_at": (NOW - timedelta(hours=1)).isoformat()})
    assert refunded["subscription_tier"] == "free", "a refund ends access now"


def test_out_of_order_and_late_events_do_not_clobber_newer_state():
    renewed = {"subscription_tier": "standard", "subscription_expires_at": (NOW + timedelta(days=30)).isoformat()}
    assert decide(event("EXPIRATION", expires=NOW - timedelta(days=1)), renewed) is None, "old expiry after a renewal"
    assert decide(event("RENEWAL", expires=NOW - timedelta(days=2)), FREE) is None, "a renewal for a period that already ended"
    assert decide(event("RENEWAL", expires=NOW + timedelta(days=3)), renewed) is None, "an earlier period arriving late"


def test_upgrade_now_downgrade_at_renewal():
    standard = {"subscription_tier": "standard", "subscription_expires_at": (NOW + timedelta(days=10)).isoformat()}
    assert decide(event("PRODUCT_CHANGE", ("pro",)), standard)["subscription_tier"] == "pro"
    pro = {"subscription_tier": "pro", "subscription_expires_at": (NOW + timedelta(days=10)).isoformat()}
    assert decide(event("PRODUCT_CHANGE", ("standard",)), pro) is None


def test_effective_tier_expires_with_a_short_grace():
    assert subs.effective_tier(FREE, NOW) == "free"
    assert subs.effective_tier({"subscription_tier": "pro", "subscription_expires_at": (NOW + timedelta(days=1)).isoformat()}, NOW) == "pro"
    assert subs.effective_tier({"subscription_tier": "pro", "subscription_expires_at": (NOW - timedelta(days=1)).isoformat()}, NOW) == "pro", "late webhook grace"
    assert subs.effective_tier({"subscription_tier": "pro", "subscription_expires_at": (NOW - timedelta(days=3)).isoformat()}, NOW) == "free"
    assert subs.effective_tier({"subscription_tier": "standard", "subscription_expires_at": None}, NOW) == "standard", "no expiry = lifetime or granted"
    assert subs.effective_tier({"subscription_tier": "gold"}, NOW) == "free"


def test_tier_from_revenuecat_subscriber():
    sub = {
        "entitlements": {
            "standard": {"expires_date": (NOW + timedelta(days=20)).isoformat()},
            "pro": {"expires_date": (NOW - timedelta(days=2)).isoformat()},
            "other": {"expires_date": None},
        }
    }
    out = subs.tier_from_subscriber(sub, "standard", "pro", NOW)
    assert out["subscription_tier"] == "standard" and subs.parse_time(out["subscription_expires_at"]) == NOW + timedelta(days=20)
    assert subs.tier_from_subscriber({"entitlements": {"pro": {"expires_date": None}}}, "standard", "pro", NOW) == {"subscription_tier": "pro", "subscription_expires_at": None}
    in_grace = {"entitlements": {"pro": {"expires_date": (NOW - timedelta(days=1)).isoformat(), "grace_period_expires_date": (NOW + timedelta(days=5)).isoformat()}}}
    assert subs.tier_from_subscriber(in_grace, "standard", "pro", NOW)["subscription_tier"] == "pro"
    assert subs.tier_from_subscriber({}, "standard", "pro", NOW)["subscription_tier"] == "free"
