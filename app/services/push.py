"""Phone notifications (web push), written in the user's companion's voice.

Sending runs on a background thread and never raises: a notification is a
bonus, never a reason for the action that caused it to fail. Subscriptions
that the push service says are gone (404/410) are deleted.

Needs VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY and VAPID_SUBJECT in the
environment; without them nothing is sent.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from supabase import Client

# Mirrors the app's roster (src/components/companions/roster.ts).
COMPANIONS: list[tuple[int, str, str]] = [
    (0, "bee", "Nahl"),
    (100, "cat", "Muezza"),
    (200, "ant", "Naml"),
    (350, "hoopoe", "Hudhud"),
    (500, "dove", "Hamama"),
    (750, "camel", "Naqa"),
    (1000, "cow", "Baqara"),
    (1500, "elephant", "Fil"),
    (2000, "lion", "Layth"),
    (3000, "whale", "Hut"),
]
NAMES = {cid: name for _, cid, name in COMPANIONS}


def unlocked_between(before: int, after: int) -> list[tuple[str, str]]:
    """Companions whose threshold was crossed going from `before` to `after` points."""

    return [(cid, name) for pts, cid, name in COMPANIONS if before < pts <= after]


def build_payload(title: str, body: str, url: str, tag: str | None, companion: str | None) -> dict:
    """The JSON the service worker turns into a notification.

    `{companion}` in the title or body becomes the user's companion's name.
    """

    cid = companion if companion in NAMES else "bee"
    name = NAMES[cid]
    return {
        "title": title.replace("{companion}", name)[:80],
        "body": body.replace("{companion}", name)[:240],
        "url": url if url.startswith("/") else "/",
        "tag": tag,
        "icon": f"/companions/{cid}.png",
    }


def configured() -> bool:
    from app.config import get_settings

    s = get_settings()
    return bool(getattr(s, "VAPID_PUBLIC_KEY", "") and getattr(s, "VAPID_PRIVATE_KEY", ""))


def notify(
    supabase: "Client",
    user_ids: Iterable[str],
    title: str,
    body: str,
    url: str = "/",
    tag: str | None = None,
) -> None:
    """Send a notification to every device of these users, in the background."""

    ids = sorted({u for u in user_ids if u})
    if not ids or not configured():
        return
    threading.Thread(target=_send, args=(supabase, ids, title, body, url, tag), daemon=True).start()


def _send(supabase: "Client", ids: list[str], title: str, body: str, url: str, tag: str | None) -> None:
    try:
        from pywebpush import WebPushException, webpush

        from app.config import get_settings

        settings = get_settings()
        subs = (
            supabase.table("push_subscriptions").select("id,user_id,endpoint,p256dh,auth").in_("user_id", ids).execute().data
            or []
        )
        if not subs:
            return
        companions = {
            row["id"]: row.get("active_companion")
            for row in (
                supabase.table("user_profiles").select("id,active_companion").in_("id", ids).execute().data or []
            )
        }
        now = datetime.now(timezone.utc).isoformat()
        for sub in subs:
            payload = build_payload(title, body, url, tag, companions.get(sub["user_id"]))
            try:
                webpush(
                    subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                    data=json.dumps(payload),
                    vapid_private_key=settings.VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": settings.VAPID_SUBJECT},
                    ttl=24 * 60 * 60,
                )
                supabase.table("push_subscriptions").update({"last_sent_at": now}).eq("id", sub["id"]).execute()
            except WebPushException as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (404, 410):
                    supabase.table("push_subscriptions").delete().eq("id", sub["id"]).execute()
                else:
                    logging.warning("Push send failed (%s): %s", status, exc)
    except Exception as exc:  # never let a notification break anything
        logging.warning("Push notifications unavailable: %s", exc)


# ── Who to tell ──────────────────────────────────────────────────────────────


def product_followers(supabase: "Client", product_id: str) -> set[str]:
    """People who saved the product or asked us to contact its manufacturer."""

    ids: set[str] = set()
    for table in ("user_favourites", "product_watchers"):
        try:
            rows = supabase.table(table).select("user_id").eq("product_id", product_id).execute().data or []
            ids.update(r["user_id"] for r in rows)
        except Exception:
            pass
    return ids
