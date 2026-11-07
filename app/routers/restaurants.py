"""Halal places to eat: discovery, reports, suggestions and reviews."""

from math import cos, radians
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from supabase import Client

from app.database import get_current_user, get_supabase, is_admin
from app.models.restaurant import RestaurantCreate, RestaurantReviewCreate
from app.ratelimit import rate_limit
from app.services.overpass import fetch_halal_food
from app.services.places import classify_osm, from_listing, merge_places

router = APIRouter()

ATTRIBUTION = "Place data © OpenStreetMap contributors"


class PlaceReport(BaseModel):
    place_id: str = Field(min_length=3, max_length=80, pattern=r"^(osm:(node|way|relation)/\d+|tayyib:[0-9a-f-]{36})$")
    reason: Literal["not_halal", "not_restaurant", "closed", "wrong_location"]
    note: str | None = Field(default=None, max_length=300)


@router.get("/nearby", dependencies=[Depends(rate_limit("places_minute", 20, 60))])
async def nearby_places(
    lat: float = Query(ge=-90, le=90),
    lng: float = Query(ge=-180, le=180),
    radius_km: float = Query(default=8, gt=0, le=25),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Halal restaurants, fast food, cafés and food courts near a point.

    Combines listings reviewed by tayyib. with OpenStreetMap places tagged
    halal, after excluding shops, groceries, butchers, closed places and
    anything the community has reported enough times.
    """

    origin = (lat, lng)
    lat_delta = radius_km / 111.0
    lng_delta = radius_km / (111.0 * max(cos(radians(lat)), 0.01))
    rows = (
        supabase.table("restaurants")
        .select("*")
        .gte("lat", lat - lat_delta)
        .lte("lat", lat + lat_delta)
        .gte("lng", lng - lng_delta)
        .lte("lng", lng + lng_delta)
        .neq("halal_status", "unverified")
        .execute()
    ).data or []
    listings = [p for p in (from_listing(r, origin) for r in rows) if p]

    elements = await fetch_halal_food(lat, lng, int(radius_km * 1000))
    osm = [p for p in (classify_osm(e, origin) for e in elements or []) if p]

    hidden = {row["place_key"] for row in (supabase.table("hidden_places").select("place_key").execute().data or [])}
    items = merge_places(listings, osm, hidden, radius_km)
    return {
        "items": items,
        "radius_km": radius_km,
        "osm": "ok" if elements is not None else "unavailable",
        "attribution": ATTRIBUTION,
    }


@router.post("/report", dependencies=[Depends(rate_limit("place_report", 10, 3600))])
async def report_place(
    payload: PlaceReport,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Flag a place as not halal, not a restaurant, closed or misplaced.

    Three reports of not halal, not a restaurant or closed hide it from the map.
    """

    supabase.table("place_reports").upsert(
        {
            "place_key": payload.place_id,
            "user_id": user["id"],
            "reason": payload.reason,
            "note": (payload.note or "").strip() or None,
        },
        on_conflict="place_key,user_id",
    ).execute()
    return {"reported": True}


# ── Admin review ─────────────────────────────────────────────────────────


def _require_admin(user: dict, supabase: Client) -> None:
    if not is_admin(user["id"], supabase):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")


class SuggestionDecision(BaseModel):
    decision: Literal["fully_halal", "halal_options", "reject"]


class ClearReports(BaseModel):
    place_key: str = Field(min_length=6, max_length=80)


@router.get("/admin/review")
async def admin_review(
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Suggested places waiting for review, and places the community has reported."""

    _require_admin(user, supabase)
    suggestions = (
        supabase.table("restaurants")
        .select("id,name,address,cuisine_type,lat,lng,phone,website,notes,created_at")
        .eq("halal_status", "unverified")
        .order("created_at", desc=True)
        .limit(100)
        .execute()
    ).data or []
    rows = (
        supabase.table("place_reports")
        .select("place_key,reason,note,created_at")
        .order("created_at", desc=True)
        .limit(1000)
        .execute()
    ).data or []
    grouped: dict[str, dict] = {}
    for r in rows:
        g = grouped.setdefault(
            r["place_key"],
            {"place_key": r["place_key"], "total": 0, "reasons": {}, "notes": [], "latest": r["created_at"]},
        )
        g["total"] += 1
        g["reasons"][r["reason"]] = g["reasons"].get(r["reason"], 0) + 1
        if r.get("note") and len(g["notes"]) < 5:
            g["notes"].append(r["note"])
    hidden = {h["place_key"] for h in (supabase.table("hidden_places").select("place_key").execute().data or [])}
    listing_ids = [k.split(":", 1)[1] for k in grouped if k.startswith("tayyib:")]
    names = {}
    if listing_ids:
        names = {
            row["id"]: row["name"]
            for row in (supabase.table("restaurants").select("id,name").in_("id", listing_ids).execute().data or [])
        }
    reports = []
    for key, g in grouped.items():
        g["hidden"] = key in hidden
        g["name"] = names.get(key.split(":", 1)[1]) if key.startswith("tayyib:") else None
        reports.append(g)
    reports.sort(key=lambda g: g["latest"], reverse=True)
    reports.sort(key=lambda g: g["total"], reverse=True)
    return {"suggestions": suggestions, "reports": reports}


@router.post("/admin/suggestions/{restaurant_id}")
async def decide_suggestion(
    restaurant_id: str,
    payload: SuggestionDecision,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Publish a suggested place on the map, or reject (delete) it."""

    _require_admin(user, supabase)
    if payload.decision == "reject":
        supabase.table("restaurants").delete().eq("id", restaurant_id).eq("halal_status", "unverified").execute()
        return {"rejected": True}
    updated = (
        supabase.table("restaurants")
        .update({"halal_status": payload.decision})
        .eq("id", restaurant_id)
        .execute()
    )
    if not updated.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Suggestion not found.")
    return updated.data[0]


@router.post("/admin/reports/clear")
async def clear_reports(
    payload: ClearReports,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Dismiss the reports on a place, which puts it back on the map."""

    _require_admin(user, supabase)
    supabase.table("place_reports").delete().eq("place_key", payload.place_key).execute()
    return {"cleared": True}


@router.get("/{restaurant_id}")
async def get_restaurant(
    restaurant_id: str,
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Return a restaurant with reviews."""

    response = (
        supabase.table("restaurants")
        .select("*, restaurant_reviews(*)")
        .eq("id", restaurant_id)
        .limit(1)
        .execute()
    )
    if not response.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Restaurant not found.",
        )
    return response.data[0]


@router.post("")
async def create_restaurant(
    payload: RestaurantCreate,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Create a restaurant listing."""

    listing = payload.model_dump()
    listing["submitted_by"] = user["id"]
    # Users can suggest a listing, but only an admin can mark it certified or halal.
    listing["halal_status"] = "unverified"
    response = supabase.table("restaurants").insert(listing).execute()
    if not response.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create restaurant.",
        )
    return response.data[0]


@router.post("/{restaurant_id}/reviews")
async def add_restaurant_review(
    restaurant_id: str,
    payload: RestaurantReviewCreate,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Add a restaurant review and update average rating."""

    review_payload = payload.model_dump()
    review_payload.update({"restaurant_id": restaurant_id, "user_id": user["id"]})
    review = supabase.table("restaurant_reviews").insert(review_payload).execute()
    if not review.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create review.",
        )

    reviews = (
        supabase.table("restaurant_reviews")
        .select("rating")
        .eq("restaurant_id", restaurant_id)
        .execute()
    )
    ratings = [int(item["rating"]) for item in reviews.data or []]
    avg_rating = round(sum(ratings) / len(ratings), 2) if ratings else None
    supabase.table("restaurants").update(
        {"avg_rating": avg_rating, "review_count": len(ratings)}
    ).eq("id", restaurant_id).execute()
    return review.data[0]
