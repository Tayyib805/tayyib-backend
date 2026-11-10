"""Halal places to eat: the map, what shoppers tell us about places, and review.

Places are stored (see migration 022). Every halal label on a place is worked
out by the database from dated evidence: this router only reads places and
adds evidence. Nothing here sets a label directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from supabase import Client

from app.config import get_settings
from app.database import get_current_user, get_supabase, is_admin
from app.models.restaurant import RestaurantCreate, RestaurantReviewCreate
from app.ratelimit import rate_limit
from app.services import places_store
from app.services.places import looks_like_grocery, present_place
from app.services.points import award_points, under_daily_cap
from app.services.trust import vote_counts

log = logging.getLogger("tayyib.places")
router = APIRouter()

ATTRIBUTION = "Place data © OpenStreetMap contributors, Overture Maps Foundation and tayyib. shoppers"
UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
PLACE_ANSWER_POINTS = 5
DAILY_PLACE_AWARDS = 10
FIRST_READ_WAIT_SECONDS = 12


def _who(user_id: str) -> str:
    """Stable, non-identifying reference for "the same person" on evidence rows."""

    return hashlib.md5(user_id.encode()).hexdigest()


def _place(supabase: Client, place_id: str) -> dict:
    rows = supabase.table("places").select("*").eq("id", place_id).limit(1).execute().data
    if not rows or rows[0].get("status") == "hidden":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We couldn't find that place.")
    return rows[0]


def _add_evidence(supabase: Client, place_id: str, answers: dict[str, str], source: str, who: str, user_id: str | None, note: str | None = None) -> None:
    """Add or replace this voice's answers. An answer an admin retracted stays retracted."""

    rows = [
        {
            "place_id": place_id,
            "attribute": attribute,
            "value": value,
            "source": source,
            "source_ref": who,
            "user_id": user_id,
            "note": note,
            "observed_at": places_store._now().isoformat(),
        }
        for attribute, value in answers.items()
    ]
    if rows:
        supabase.table("place_evidence").upsert(rows, on_conflict="place_id,attribute,source,source_ref").execute()


# ── The map ──────────────────────────────────────────────────────────────


@router.get("/nearby", dependencies=[Depends(rate_limit("places_minute", 30, 60))])
async def nearby_places(
    lat: float = Query(ge=-90, le=90),
    lng: float = Query(ge=-180, le=180),
    radius_km: float = Query(default=8, gt=0, le=25),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Stored halal places near a point, best-evidenced first.

    Answers from the database. If this map square has never been read from
    OpenStreetMap, it is read now (once); if its copy is over two weeks old,
    it is refreshed in the background.
    """

    def read() -> list[dict]:
        rows = supabase.rpc(
            "places_near", {"p_lat": lat, "p_lng": lng, "p_radius_m": int(radius_km * 1000), "p_limit": 200}
        ).execute().data or []
        return [present_place(r) for r in rows]

    if places_store.open_data_due():
        places_store.open_data_in_background(supabase, get_settings().PLACES_DATA_URL)
    cell = places_store.cell_of(lat, lng)
    cell_row = places_store.note_view(supabase, cell)
    osm = "ok"
    items = read()
    if places_store.needs_sync(cell_row):
        never_synced = not (cell_row or {}).get("last_synced_at")
        task = places_store.sync_in_background(supabase, cell) if places_store.can_attempt(cell) else None
        if never_synced and not items:
            # A brand-new area: give the first read a few seconds so the map isn't empty.
            if task is not None:
                await asyncio.wait({task}, timeout=FIRST_READ_WAIT_SECONDS)
            if task is not None and task.done() and not task.cancelled() and task.exception() is None and task.result():
                items = read()
            elif places_store.is_running(cell):
                osm = "loading"
            else:
                osm = "unavailable"
    items.sort(key=lambda p: p["distance_km"] if p["distance_km"] is not None else 1e9)
    return {"items": items, "radius_km": radius_km, "osm": osm, "attribution": ATTRIBUTION}


# ── What shoppers tell us ────────────────────────────────────────────────


class PlaceAnswers(BaseModel):
    """One tap ("still halal?") or a few details. Each is optional."""

    halal: Literal["yes", "no"] | None = None
    open: Literal["yes", "closed"] | None = None
    halal_menu: Literal["full", "partial"] | None = None
    slaughter: Literal["hand", "machine", "mixed"] | None = None
    alcohol: Literal["yes", "no"] | None = None
    pork: Literal["yes", "no"] | None = None
    prayer: Literal["designated", "on_request", "none"] | None = None
    note: str | None = Field(default=None, max_length=300)

    def answers(self) -> dict[str, str]:
        data = self.model_dump(exclude_none=True)
        data.pop("note", None)
        return data


@router.post("/{place_id}/evidence", dependencies=[Depends(rate_limit("place_evidence", 40, 3600))])
async def answer_about_place(
    place_id: str,
    payload: PlaceAnswers,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Record what a shopper knows about a place. Their newest answer replaces their older one."""

    place = _place(supabase, place_id)
    answers = payload.answers()
    if not answers:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose at least one answer.")
    if answers.get("halal") == "yes" and "open" not in answers:
        answers["open"] = "yes"  # they were just there
    who = _who(user["id"])
    first_time = not (
        supabase.table("place_evidence").select("id").eq("place_id", place_id).eq("source", "user").eq("source_ref", who).limit(1).execute().data
    )
    can_award = first_time and under_daily_cap(supabase, "place_evidence", user["id"], DAILY_PLACE_AWARDS * 3)
    _add_evidence(supabase, place_id, answers, "user", who, user["id"], (payload.note or "").strip() or None)
    points = 0
    if can_award:
        award_points(supabase, user["id"], PLACE_ANSWER_POINTS)
        points = PLACE_ANSWER_POINTS
    fresh = supabase.rpc("places_near", {"p_lat": place["lat"], "p_lng": place["lng"], "p_radius_m": 50, "p_limit": 20}).execute().data or []
    current = next((present_place(r) for r in fresh if r["id"] == place_id), None)
    return {"saved": True, "points_awarded": points, "place": current}


class PlaceReport(BaseModel):
    place_id: str = Field(pattern=UUID)
    reason: Literal["not_halal", "not_restaurant", "closed", "wrong_location"]
    note: str | None = Field(default=None, max_length=300)


@router.post("/report", dependencies=[Depends(rate_limit("place_report", 10, 3600))])
async def report_place(
    payload: PlaceReport,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Say a place is not halal, closed, not somewhere to eat, or in the wrong spot.

    "Not halal" and "closed" count as evidence, weighed with everyone else's.
    The other two go to review.
    """

    _place(supabase, payload.place_id)
    note = (payload.note or "").strip() or None
    if payload.reason in ("not_halal", "closed"):
        answers = {"halal": "no"} if payload.reason == "not_halal" else {"open": "closed"}
        _add_evidence(supabase, payload.place_id, answers, "user", _who(user["id"]), user["id"], note)
    else:
        existing = (
            supabase.table("place_flags").select("id").eq("place_id", payload.place_id).eq("user_id", user["id"])
            .eq("reason", payload.reason).is_("resolved_at", "null").limit(1).execute().data
        )
        if not existing:
            supabase.table("place_flags").insert(
                {"place_id": payload.place_id, "user_id": user["id"], "reason": payload.reason, "note": note}
            ).execute()
    return {"reported": True}


class PlaceCreate(RestaurantCreate):
    """A place a shopper adds, with what they know about it."""

    halal_menu: Literal["full", "partial"] | None = None
    alcohol: Literal["yes", "no"] | None = None
    slaughter: Literal["hand", "machine", "mixed"] | None = None


@router.post("", dependencies=[Depends(rate_limit("suggest_place", 10, 86400))])
async def add_place(
    payload: PlaceCreate,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Add a halal place to eat.

    If we already have it, the answers are added to that place instead. A new
    place from an established account goes on the map as "Listed" straight
    away; from a new account it waits for a quick review first.
    """

    if payload.lat is None or payload.lng is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Drop a pin on the map so we know where it is.")
    name = " ".join(payload.name.split())
    if looks_like_grocery(name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The map lists places to eat for now. Groceries and butchers are coming later.",
        )
    who = _who(user["id"])
    answers = {"halal": "yes", "open": "yes"}
    for key in ("halal_menu", "alcohol", "slaughter"):
        if getattr(payload, key):
            answers[key] = getattr(payload, key)
    if payload.prayer_space:
        answers["prayer"] = "designated"
    note = (payload.notes or "").strip() or None

    match = supabase.rpc(
        "match_place", {"p_name": name, "p_lat": payload.lat, "p_lng": payload.lng, "p_phone": None, "p_website": payload.website}
    ).execute().data
    if match:
        _add_evidence(supabase, match, answers, "user", who, user["id"], note)
        existing = supabase.table("places").select("id,name,status").eq("id", match).limit(1).execute().data
        return {"matched": True, "live": bool(existing and existing[0]["status"] in ("active", "possibly_closed")), "id": match, "name": existing[0]["name"] if existing else name}

    live = vote_counts(supabase, user)
    cuisine = [c.strip() for c in (payload.cuisine_type or "").split(",") if c.strip()][:4]
    created = supabase.table("places").insert(
        {
            "name": name[:160],
            "lat": payload.lat,
            "lng": payload.lng,
            "address": (payload.address or "").strip() or None,
            "phone": (payload.phone or "").strip() or None,
            "website": payload.website,
            "cuisine": cuisine,
            "status": "candidate",
            "created_by": user["id"],
        }
    ).execute().data
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Couldn't save the place. Try again.")
    place_id = created[0]["id"]
    supabase.table("place_sources").insert(
        {"place_id": place_id, "source": "user", "external_id": f"{who}:{place_id}", "scope": "shoppers"}
    ).execute()
    if live:
        # Evidence from an established account moves it from candidate to the map.
        _add_evidence(supabase, place_id, answers, "user", who, user["id"], note)
    else:
        # Kept as a candidate until reviewed: their answers are stored but don't publish it.
        _add_evidence(supabase, place_id, {k: v for k, v in answers.items() if k not in ("halal", "open")}, "user", who, user["id"], note)
        supabase.table("place_flags").insert(
            {"place_id": place_id, "user_id": user["id"], "reason": "other", "note": "New place from a new account: review before it goes on the map."}
        ).execute()
    for dup in supabase.rpc("near_duplicates", {"p_name": name, "p_lat": payload.lat, "p_lng": payload.lng}).execute().data or []:
        if dup["place_id"] != place_id:
            a, b = sorted([place_id, dup["place_id"]])
            supabase.table("place_duplicates").upsert({"place_a": a, "place_b": b, "score": dup["score"]}, on_conflict="place_a,place_b").execute()
    return {"matched": False, "live": live, "id": place_id, "name": name}


@router.post("/{place_id}/reviews", dependencies=[Depends(rate_limit("place_review", 20, 86400))])
async def add_place_review(
    place_id: str,
    payload: RestaurantReviewCreate,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Rate a place (one rating per person; a new one replaces the old)."""

    _place(supabase, place_id)
    saved = supabase.table("place_reviews").upsert(
        {"place_id": place_id, "user_id": user["id"], "rating": payload.rating, "body": (payload.body or "").strip()[:1000] or None},
        on_conflict="place_id,user_id",
    ).execute().data
    return saved[0] if saved else {"saved": True}


# ── Review (admins) ──────────────────────────────────────────────────────


def _require_admin(user: dict, supabase: Client) -> None:
    if not is_admin(user["id"], supabase):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")


PLACE_FIELDS = "id,name,kind,lat,lng,address,city,phone,website,cuisine,status,tier,confidence,halal_menu,alcohol,slaughter,disagreeing_people,closed_reports,last_confirmed_at,created_at"


@router.get("/admin/review")
async def admin_review(
    lat: float | None = Query(default=None, ge=-90, le=90),
    lng: float | None = Query(default=None, ge=-180, le=180),
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Everything about places that needs a person: candidates to sort, disputes, flags, duplicates."""

    _require_admin(user, supabase)
    counts = {
        "candidates": supabase.table("places").select("id", count="exact").eq("status", "candidate").limit(1).execute().count or 0,
    }
    query = supabase.table("places").select(PLACE_FIELDS).eq("status", "candidate")
    if lat is not None and lng is not None:
        # Sort the area you're looking at first: about 30 km around the point.
        query = query.gte("lat", lat - 0.27).lte("lat", lat + 0.27).gte("lng", lng - 0.33).lte("lng", lng + 0.33)
    candidates = query.order("city").order("name").limit(60).execute().data or []

    disputed = (
        supabase.table("places").select(PLACE_FIELDS).eq("tier", "disputed").neq("status", "hidden").order("updated_at", desc=True).limit(50).execute().data
        or []
    )
    flags = (
        supabase.table("place_flags").select("id,place_id,reason,note,created_at,places(name,address,city,status,lat,lng)")
        .is_("resolved_at", "null").order("created_at", desc=True).limit(100).execute().data
        or []
    )
    dup_rows = supabase.table("place_duplicates").select("place_a,place_b,score,created_at").is_("decided", "null").limit(50).execute().data or []
    ids = list({d["place_a"] for d in dup_rows} | {d["place_b"] for d in dup_rows})
    by_id = {}
    if ids:
        by_id = {p["id"]: p for p in (supabase.table("places").select(PLACE_FIELDS).in_("id", ids).execute().data or [])}
    duplicates = [
        {"a": by_id[d["place_a"]], "b": by_id[d["place_b"]], "score": d["score"]}
        for d in dup_rows
        if d["place_a"] in by_id and d["place_b"] in by_id
    ]
    suspect = supabase.table("sync_runs").select("id,source,scope,went_missing,started_at").eq("status", "suspect").order("started_at", desc=True).limit(10).execute().data or []

    # A little evidence context for candidates and disputes, so decisions aren't blind.
    context_ids = [p["id"] for p in candidates + disputed]
    sources: dict[str, list[str]] = {}
    notes: dict[str, list[str]] = {}
    if context_ids:
        for s in supabase.table("place_sources").select("place_id,source,data").in_("place_id", context_ids).execute().data or []:
            label = s["source"]
            categories = (s.get("data") or {}).get("categories")
            if categories:
                label += f" ({categories})"
            sources.setdefault(s["place_id"], []).append(label)
        for e in (
            supabase.table("place_evidence").select("place_id,attribute,value,source,note").in_("place_id", context_ids).is_("retracted_at", "null").execute().data
            or []
        ):
            text = f"{e['source']}: {e['attribute']} {e['value']}" + (f" — {e['note']}" if e.get("note") else "")
            notes.setdefault(e["place_id"], []).append(text)
    for p in candidates + disputed:
        p["sources"] = sources.get(p["id"], [])
        p["evidence"] = notes.get(p["id"], [])[:8]
    return {"counts": counts, "candidates": candidates, "disputed": disputed, "flags": flags, "duplicates": duplicates, "suspect_syncs": suspect}


class PlaceDecision(BaseModel):
    decision: Literal["halal", "halal_full", "halal_partial", "not_halal", "closed", "not_a_place"]
    alcohol: Literal["yes", "no"] | None = None
    note: str | None = Field(default=None, max_length=300)


@router.post("/admin/places/{place_id}")
async def decide_place(
    place_id: str,
    payload: PlaceDecision,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Sort a candidate or settle a dispute. Recorded as evidence from tayyib., like everything else."""

    _require_admin(user, supabase)
    rows = supabase.table("places").select("id,status").eq("id", place_id).limit(1).execute().data
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Place not found.")
    who = _who(user["id"])
    note = (payload.note or "").strip() or None
    if payload.decision in ("halal", "halal_full", "halal_partial"):
        answers = {"halal": "yes"}
        if payload.decision != "halal":
            answers["halal_menu"] = "full" if payload.decision == "halal_full" else "partial"
        if payload.alcohol:
            answers["alcohol"] = payload.alcohol
        if rows[0]["status"] in ("hidden", "closed"):
            supabase.table("places").update({"status": "candidate"}).eq("id", place_id).execute()
        _add_evidence(supabase, place_id, answers, "admin", who, user["id"], note)
    elif payload.decision == "closed":
        _add_evidence(supabase, place_id, {"open": "closed"}, "admin", who, user["id"], note)
    else:
        # Not halal, or not somewhere to eat: off the map, with the reason kept.
        if payload.decision == "not_halal":
            _add_evidence(supabase, place_id, {"halal": "no"}, "admin", who, user["id"], note)
        supabase.table("places").update({"status": "hidden"}).eq("id", place_id).execute()
    supabase.table("place_flags").update({"resolved_at": places_store._now().isoformat()}).eq("place_id", place_id).is_("resolved_at", "null").execute()
    current = supabase.table("places").select("id,name,status,tier,confidence").eq("id", place_id).limit(1).execute().data
    return current[0] if current else {"id": place_id}


@router.post("/admin/flags/{flag_id}")
async def dismiss_flag(
    flag_id: str,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Dismiss a flag without changing the place."""

    _require_admin(user, supabase)
    supabase.table("place_flags").update({"resolved_at": places_store._now().isoformat()}).eq("id", flag_id).execute()
    return {"dismissed": True}


class DuplicateDecision(BaseModel):
    keep: str = Field(pattern=UUID)
    other: str = Field(pattern=UUID)
    decision: Literal["merge", "different"]


@router.post("/admin/duplicates")
async def decide_duplicate(
    payload: DuplicateDecision,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Merge two listings of the same place (evidence and sources move to the one kept), or mark them as different."""

    _require_admin(user, supabase)
    a, b = sorted([payload.keep, payload.other])
    if payload.decision == "merge":
        supabase.rpc("merge_places", {"keep": payload.keep, "drop_id": payload.other}).execute()
        return {"merged": True}
    supabase.table("place_duplicates").update({"decided": "different"}).eq("place_a", a).eq("place_b", b).execute()
    return {"merged": False}


@router.get("/{place_id}")
async def get_place(place_id: str, supabase: Client = Depends(get_supabase)) -> dict:
    """One place as the map shows it, with its ratings."""

    place = _place(supabase, place_id)
    if place["status"] not in ("active", "possibly_closed"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We couldn't find that place.")
    if place.get("certifier_id"):
        cert = supabase.table("certifiers").select("name").eq("id", place["certifier_id"]).limit(1).execute().data
        place["certifier"] = cert[0]["name"] if cert else None
    item = present_place(place)
    reviews = supabase.table("place_reviews").select("rating,body,created_at").eq("place_id", place_id).order("created_at", desc=True).limit(30).execute().data or []
    ratings = [int(r["rating"]) for r in reviews]
    item["avg_rating"] = round(sum(ratings) / len(ratings), 1) if ratings else None
    item["review_count"] = len(ratings)
    item["reviews"] = reviews
    return item
