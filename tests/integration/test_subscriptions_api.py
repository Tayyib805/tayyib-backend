"""RevenueCat webhook against the real schema."""

import time
import types

import pytest

from tests.integration.conftest import run


def settings(**over):
    base = dict(
        REVENUECAT_WEBHOOK_AUTH="Bearer hook-secret",
        REVENUECAT_API_KEY="",
        REVENUECAT_ENTITLEMENT_STANDARD="standard",
        REVENUECAT_ENTITLEMENT_PRO="pro",
        REVENUECAT_ACCEPT_SANDBOX=False,
    )
    return types.SimpleNamespace(**{**base, **over})


@pytest.fixture()
def hook(pg, monkeypatch):
    from app.routers import platform

    monkeypatch.setattr(platform, "get_settings", lambda: settings())
    return platform


def ev(event_id, kind, user_id, days=30, entitlements=("standard",), **extra):
    now = int(time.time() * 1000)
    return {"api_version": "1.0", "event": {"id": event_id, "type": kind, "app_user_id": user_id, "entitlement_ids": list(entitlements),
                                             "expiration_at_ms": now + days * 86400000, "event_timestamp_ms": now, "store": "APP_STORE",
                                             "environment": "PRODUCTION", "product_id": "standard_monthly", **extra}}


def profile(pg, uid):
    return pg.run(f"select row_to_json(t) from (select subscription_tier, subscription_expires_at, subscription_store from public.user_profiles where id = '{uid}') t")


def status_of(call):
    with pytest.raises(Exception) as err:
        run(call)
    return getattr(err.value, "status_code", None)


def test_webhook_is_refused_until_configured_and_without_the_right_header(pg, hook, people, monkeypatch):
    user = people()
    assert status_of(hook.revenuecat_webhook(ev("x1", "INITIAL_PURCHASE", user["id"]), authorization=None, supabase=pg)) == 401
    assert status_of(hook.revenuecat_webhook(ev("x1", "INITIAL_PURCHASE", user["id"]), authorization="Bearer wrong", supabase=pg)) == 401
    monkeypatch.setattr(hook, "get_settings", lambda: settings(REVENUECAT_WEBHOOK_AUTH=""))
    assert status_of(hook.revenuecat_webhook(ev("x1", "INITIAL_PURCHASE", user["id"]), authorization="Bearer hook-secret", supabase=pg)) == 503
    assert profile(pg, user["id"])["subscription_tier"] == "free"


def test_purchase_renewal_expiry_and_repeats(pg, hook, people):
    user = people()
    auth = "Bearer hook-secret"
    out = run(hook.revenuecat_webhook(ev("p1", "INITIAL_PURCHASE", user["id"]), authorization=auth, supabase=pg))
    assert out == {"received": True, "applied": True}
    assert profile(pg, user["id"])["subscription_tier"] == "standard" and profile(pg, user["id"])["subscription_store"] == "APP_STORE"

    again = run(hook.revenuecat_webhook(ev("p1", "EXPIRATION", user["id"], days=-1), authorization=auth, supabase=pg))
    assert again["applied"] is False and again["reason"] == "already processed"
    assert profile(pg, user["id"])["subscription_tier"] == "standard", "a repeated event id is ignored"

    run(hook.revenuecat_webhook(ev("p2", "PRODUCT_CHANGE", user["id"], entitlements=("pro",)), authorization=auth, supabase=pg))
    assert profile(pg, user["id"])["subscription_tier"] == "pro"
    run(hook.revenuecat_webhook(ev("p3", "CANCELLATION", user["id"]), authorization=auth, supabase=pg))
    assert profile(pg, user["id"])["subscription_tier"] == "pro", "cancelling keeps access until the period ends"
    pg.run(f"with u as (update public.user_profiles set subscription_expires_at = now() - interval '1 minute' where id = '{user['id']}' returning 1) select to_json(count(*)) from u")
    run(hook.revenuecat_webhook(ev("p4", "EXPIRATION", user["id"], days=0), authorization=auth, supabase=pg))
    assert profile(pg, user["id"])["subscription_tier"] == "free"
    logged = pg.run(f"select to_json(count(*)) from public.subscription_events where user_id = '{user['id']}'")
    assert logged == 4


def test_sandbox_ignored_unknown_account_logged(pg, hook, people):
    user = people()
    auth = "Bearer hook-secret"
    out = run(hook.revenuecat_webhook(ev("s1", "INITIAL_PURCHASE", user["id"], environment="SANDBOX"), authorization=auth, supabase=pg))
    assert out["applied"] is False and profile(pg, user["id"])["subscription_tier"] == "free"
    out = run(hook.revenuecat_webhook(ev("s2", "INITIAL_PURCHASE", "$RCAnonymousID:abc"), authorization=auth, supabase=pg))
    assert out == {"received": True, "applied": False}
    assert pg.run("select to_json(user_id) from public.subscription_events where id = 's2'") is None


def test_with_an_api_key_revenuecat_is_the_source_of_truth(pg, hook, people, monkeypatch):
    user = people()
    monkeypatch.setattr(hook, "get_settings", lambda: settings(REVENUECAT_API_KEY="sk_test"))

    async def subscriber(app_user_id, api_key):
        assert app_user_id == user["id"] and api_key == "sk_test"
        return {"entitlements": {"pro": {"expires_date": "2099-01-01T00:00:00Z"}}}

    monkeypatch.setattr(hook.subs, "fetch_subscriber", subscriber)
    # The event itself says "standard", but RevenueCat's current state says pro.
    run(hook.revenuecat_webhook(ev("k1", "BILLING_ISSUE", user["id"]), authorization="Bearer hook-secret", supabase=pg))
    assert profile(pg, user["id"])["subscription_tier"] == "pro"


def test_clients_cannot_change_their_own_tier(pg, people):
    user = people()
    with pytest.raises(RuntimeError) as err:
        pg.run(
            "begin; select set_config('request.jwt.claim.role', 'authenticated', true); "
            f"update public.user_profiles set subscription_tier = 'pro' where id = '{user['id']}'; commit;"
        )
    assert "only be changed by the server" in str(err.value)


def test_profile_reports_the_effective_tier_and_tidies_an_expired_one(pg, people):
    from app.routers import users

    user = people()
    me = run(users.get_me(user=user, supabase=pg))
    assert me["subscription"] == {"tier": "free", "active": False, "expires_at": None, "store": None}
    pg.run(f"with u as (update public.user_profiles set subscription_tier = 'pro', subscription_expires_at = now() + interval '20 days', subscription_store = 'STRIPE' where id = '{user['id']}' returning 1) select to_json(count(*)) from u")
    me = run(users.get_me(user=user, supabase=pg))
    assert me["subscription"]["tier"] == "pro" and me["subscription"]["active"] and me["subscription"]["store"] == "STRIPE"
    pg.run(f"with u as (update public.user_profiles set subscription_expires_at = now() - interval '5 days' where id = '{user['id']}' returning 1) select to_json(count(*)) from u")
    me = run(users.get_me(user=user, supabase=pg))
    assert me["subscription"]["tier"] == "free" and profile(pg, user["id"])["subscription_tier"] == "free"
