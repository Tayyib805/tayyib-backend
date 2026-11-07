"""Decide whose community votes count toward changing a product.

Email confirmation is off, so anyone can make throwaway accounts. A vote from
a brand-new or inactive account is still recorded (so nobody can vote twice),
but only established accounts move the counters that trigger reviews and tier
changes. Admins always count.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from supabase import Client

MIN_ACCOUNT_AGE_DAYS = 7
MIN_SCANS = 3


def account_age_days(created_at: str | datetime | None, now: datetime | None = None) -> float:
    """Days since the account was created; 0 when unknown."""

    if not created_at:
        return 0.0
    if isinstance(created_at, str):
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    else:
        created = created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0.0, ((now or datetime.now(timezone.utc)) - created).total_seconds() / 86400)


def is_established(age_days: float, scan_count: int, admin: bool = False) -> bool:
    """Pure rule, kept separate so it can be tested without a database."""

    return admin or (age_days >= MIN_ACCOUNT_AGE_DAYS and scan_count >= MIN_SCANS)


def vote_counts(supabase: Client, user: dict) -> bool:
    """True when this user's vote should move community counters."""

    from app.database import is_admin

    try:
        if is_admin(user["id"], supabase):
            return True
        scans = (
            supabase.table("scan_logs")
            .select("id", count="exact")
            .eq("user_id", user["id"])
            .limit(1)
            .execute()
        )
        return is_established(account_age_days(user.get("created_at")), int(scans.count or 0))
    except Exception:
        # If we can't tell, don't let the vote change anything.
        return False


def why_not_counted() -> str:
    return (
        f"Thanks, your vote is recorded. Votes start counting toward reviews once your account "
        f"is {MIN_ACCOUNT_AGE_DAYS} days old and you've scanned {MIN_SCANS} products."
    )
