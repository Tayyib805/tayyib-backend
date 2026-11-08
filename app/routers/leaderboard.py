"""Scan leaderboard: who checked the most different products."""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from supabase import Client

from app.database import get_optional_user, get_supabase
from app.ratelimit import rate_limit
from app.services.article_text import clean_display_name
from app.services.push import NAMES

router = APIRouter()


@router.get("/scans", dependencies=[Depends(rate_limit("leaderboard", 60, 60))])
async def scan_leaderboard(
    period: Literal["day", "week", "month", "all"] = "week",
    tz: str = Query(default="UTC", max_length=64, pattern=r"^[A-Za-z0-9_+\-/]+$"),
    limit: int = Query(default=20, ge=3, le=50),
    user: dict | None = Depends(get_optional_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Top scanners for the period, plus the caller's own place if signed in."""

    rows = (
        supabase.rpc(
            "scan_leaderboard",
            {"p_period": period, "p_tz": tz, "p_limit": limit, "p_user": user["id"] if user else None},
        ).execute()
    ).data or []
    entries = []
    me = None
    for i, r in enumerate(rows):
        entry = {
            "rank": int(r["rank"]),
            "name": clean_display_name(r.get("display_name")) or f"Shopper {r['tag']}",
            "companion": r.get("active_companion") if r.get("active_companion") in NAMES else "bee",
            "products": int(r["products"]),
            "is_me": bool(r.get("is_me")),
        }
        if entry["is_me"]:
            me = entry
        if i < limit:
            entries.append(entry)
    return {"period": period, "entries": entries, "me": me}
