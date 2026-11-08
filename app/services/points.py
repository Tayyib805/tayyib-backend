"""Award companion points for useful actions.

Points live in user_profiles.points, which users can't change themselves
(migration 008 trigger); only this service, running with the service key,
adds to them. Awards never block the action that earned them.
"""

from datetime import datetime, timezone

from supabase import Client

SCAN_NEW_PRODUCT = 5
LABEL_PHOTO = 15
CONFIRM_RESULT = 10

DAILY_SCAN_AWARDS = 30
DAILY_LABEL_AWARDS = 20
DAILY_CONFIRM_AWARDS = 10


def _start_of_day() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def award_points(supabase: Client, user_id: str, amount: int) -> None:
    """Add points to a user's profile, creating the profile row if needed."""

    try:
        rows = supabase.table("user_profiles").select("points").eq("id", user_id).limit(1).execute().data
        current = int((rows[0].get("points") if rows else 0) or 0)
        supabase.table("user_profiles").upsert({"id": user_id, "points": current + amount}, on_conflict="id").execute()
        from app.services.push import notify, unlocked_between

        for _cid, name in unlocked_between(current, current + amount):
            notify(
                supabase,
                [user_id],
                f"You unlocked {name}!",
                f"{name} wants to meet you. Say hi in Rewards.",
                "/?open=rewards",
                f"unlock-{_cid}",
            )
    except Exception:
        # Points are a bonus; never fail the user's action over them.
        pass


def first_scan_of_product(supabase: Client, user_id: str, product_id: str) -> bool:
    """True if the user has never logged a scan of this product."""

    rows = (
        supabase.table("scan_logs").select("id").eq("user_id", user_id).eq("product_id", product_id).limit(1).execute()
    ).data
    return not rows


def under_daily_cap(supabase: Client, table: str, user_id: str, cap: int, extra_filter: str | None = None) -> bool:
    """True while today's count of the user's rows in `table` is below `cap`."""

    column = "scanned_at" if table == "scan_logs" else "created_at"
    query = supabase.table(table).select("id", count="exact").eq("user_id", user_id).gte(column, _start_of_day())
    if extra_filter == "with_product":
        query = query.not_.is_("product_id", "null")
    return int(query.limit(1).execute().count or 0) < cap
