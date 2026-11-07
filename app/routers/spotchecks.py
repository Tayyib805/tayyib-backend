"""Label spot-checks: does the label in the shop still match what we hold?

The rules for what a photo can change live in app/services/label_qc.py:
a matching photo verifies or confirms; a different label is stored
unconfirmed and changes nothing until three accounts match it, an admin
confirms it, or the manufacturer's list does.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from supabase import Client

from app.database import get_current_user, get_supabase, is_admin
from app.ratelimit import rate_limit
from app.routers.products import (
    ALLOWED_IMAGE_TYPES,
    MAX_IMAGE_BASE64_CHARS,
    _build_scan_response,
    _fetch_latest_formulation,
    _fetch_product_with_ingredients,
    _normalize_confidence,
    _normalize_country_code,
)
from app.services.claude_analysis import describe_error, extract_ingredients_from_image
from app.services.label_qc import CONFIRMATIONS_NEEDED, confirm_formulation, confirmations, outcome_message, process_label
from app.services.points import DAILY_LABEL_AWARDS, LABEL_PHOTO, award_points, under_daily_cap

router = APIRouter()
log = logging.getLogger("tayyib.spotcheck")


class LabelCheck(BaseModel):
    image_base64: str = Field(min_length=100)
    media_type: str = "image/jpeg"
    country_code: str = "US"


class ChangeDecision(BaseModel):
    decision: Literal["confirm", "revert"]
    review_notes: str = Field(default="", max_length=1000)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _award(supabase: Client, user_id: str) -> int:
    # The photo's own row is already saved, so allow one more than the cap.
    if under_daily_cap(supabase, "label_scans", user_id, DAILY_LABEL_AWARDS + 1):
        award_points(supabase, user_id, LABEL_PHOTO)
        return LABEL_PHOTO
    return 0


@router.post(
    "/{product_id}/label-check",
    dependencies=[
        Depends(rate_limit("label_minute", 6, 60)),
        Depends(rate_limit("label_day", 60, 86_400)),
    ],
)
async def label_check(
    product_id: str,
    payload: LabelCheck,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Compare a photo of the label with the ingredients we hold."""

    if payload.media_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use a JPEG, PNG, WebP or GIF photo.")
    if len(payload.image_base64) > MAX_IMAGE_BASE64_CHARS:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Please use a photo under 5 MB.")

    product = _fetch_product_with_ingredients(supabase, "id", product_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")
    country = _normalize_country_code(payload.country_code)
    if not _fetch_latest_formulation(supabase, product_id, country):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We don't hold this product's ingredients yet.")

    try:
        extraction = await extract_ingredients_from_image(payload.image_base64, payload.media_type)
    except Exception as exc:
        log.error("Label reading failed: %s", describe_error(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="We couldn't read labels just now. Try again in a minute.",
        )
    extraction = extraction if isinstance(extraction, dict) else {}
    label_text = extraction.get("ingredients_text")
    unreadable = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="We couldn't see an ingredients list. Photograph just the ingredients, close up and in good light.",
    )
    if not extraction.get("found") or not label_text:
        raise unreadable

    outcome = await process_label(
        supabase,
        product=product,
        country=country,
        user_id=user["id"],
        text=label_text,
        confidence=_normalize_confidence(extraction.get("confidence")),
    )
    if outcome.get("unreadable"):
        raise unreadable
    points = _award(supabase, user["id"]) if outcome.get("first_time") else 0

    result = {k: v for k, v in outcome.items() if k != "product"}
    result["points_awarded"] = points
    result["message"] = outcome_message(outcome)
    if outcome.get("applied") and outcome.get("product"):
        enriched = outcome["product"]
        enriched["country_code"] = country
        result["verdict"] = _build_scan_response(supabase, enriched, country)
    return result


# ── Admin review ─────────────────────────────────────────────────────────────


def _require_admin(user: dict, supabase: Client) -> None:
    if not is_admin(user["id"], supabase):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admins only.")


@router.get("/admin/label-changes")
async def pending_changes(user: dict = Depends(get_current_user), supabase: Client = Depends(get_supabase)) -> list[dict]:
    """Recipe changes spotted from label photos, waiting for a decision."""

    _require_admin(user, supabase)
    rows = (
        supabase.table("formulation_changes")
        .select("*, products(name,brand,barcode,verification_tier,overall_status)")
        .eq("status", "pending")
        .order("created_at", desc=True)
        .limit(100)
        .execute()
    ).data or []
    ids = [i for r in rows for i in (r.get("old_formulation_id"), r.get("new_formulation_id")) if i]
    texts = {}
    if ids:
        texts = {
            f["id"]: f.get("ingredients_text")
            for f in (supabase.table("product_formulations").select("id,ingredients_text").in_("id", ids).execute().data or [])
        }
    for r in rows:
        r["old_text"] = texts.get(r.get("old_formulation_id"))
        r["new_text"] = texts.get(r.get("new_formulation_id"))
        r.pop("analysis", None)
        r["photos"] = confirmations(supabase, r["new_formulation_id"]) if r.get("new_formulation_id") else 0
        r["needed"] = CONFIRMATIONS_NEEDED
        # Shoppers whose label still matched the old list after this was reported.
        r["old_list_photos"] = 0
        if r.get("old_formulation_id"):
            since = (
                supabase.table("label_scans")
                .select("user_id")
                .eq("formulation_id", r["old_formulation_id"])
                .eq("verified", True)
                .gte("created_at", r["created_at"])
                .execute()
            ).data or []
            r["old_list_photos"] = len({x["user_id"] for x in since if x.get("user_id")})
    return rows


@router.post("/admin/label-changes/{change_id}")
async def decide_change(
    change_id: str,
    payload: ChangeDecision,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Confirm the recipe really changed, or revert a misread photo."""

    _require_admin(user, supabase)
    rows = supabase.table("formulation_changes").select("*").eq("id", change_id).limit(1).execute().data
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Change not found.")
    change = rows[0]
    if change["status"] != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This change has already been decided.")
    product = _fetch_product_with_ingredients(supabase, "id", change["product_id"]) or {"id": change["product_id"]}

    if payload.decision == "confirm":
        formulation = None
        if change.get("new_formulation_id"):
            rows_f = supabase.table("product_formulations").select("*").eq("id", change["new_formulation_id"]).limit(1).execute().data
            formulation = rows_f[0] if rows_f else None
        if formulation:
            done = await confirm_formulation(supabase, product, formulation, by="admin")
            if done.get("verdict_pending"):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Confirmed, but we couldn't check the new ingredients just now. Try again in a minute.",
                )
    else:
        # The photo was wrong: drop its reading and reopen what we had.
        if change.get("new_formulation_id"):
            supabase.table("label_scans").update({"verified": False}).eq("formulation_id", change["new_formulation_id"]).execute()
            supabase.table("product_formulations").delete().eq("id", change["new_formulation_id"]).execute()
        if change.get("old_formulation_id"):
            supabase.table("product_formulations").update({"effective_to": None}).eq("id", change["old_formulation_id"]).execute()
        if change.get("applied") and change.get("old_formulation_id"):
            _restore_previous(supabase, change)
        elif change.get("applied"):
            # A product known only from that photo: forget the reading, so
            # the next scan asks for a label photo again.
            supabase.table("products").update(
                {"analyzed_at": None, "ingredients_text": None, "overall_status": "uncertain", "confidence_level": "low"}
            ).eq("id", change["product_id"]).execute()

    supabase.table("formulation_changes").update(
        {
            "status": "confirmed" if payload.decision == "confirm" else "reverted",
            "review_notes": payload.review_notes or None,
            "reviewed_at": _now(),
        }
    ).eq("id", change_id).execute()
    return {"status": "confirmed" if payload.decision == "confirm" else "reverted"}


def _restore_previous(supabase: Client, change: dict) -> None:
    """Put back the verdict from before an automatically applied change."""

    runs = (
        supabase.table("analysis_runs")
        .select("*")
        .eq("product_id", change["product_id"])
        .eq("product_formulation_id", change["old_formulation_id"])
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    ).data if change.get("old_formulation_id") else []
    previous = runs[0] if runs else None
    restore = {
        "overall_status": (previous or {}).get("overall_status") or change.get("old_status") or "uncertain",
        "confidence_level": (previous or {}).get("confidence_level") or "low",
        "analyzed_at": _now(),
    }
    old_text = (previous or {}).get("input_text")
    if not old_text and change.get("old_formulation_id"):
        old_text = (
            supabase.table("product_formulations").select("ingredients_text").eq("id", change["old_formulation_id"]).limit(1).execute().data
            or [{}]
        )[0].get("ingredients_text")
    if old_text:
        restore["ingredients_text"] = old_text
    supabase.table("products").update(restore).eq("id", change["product_id"]).execute()
    if previous:
        supabase.table("analysis_runs").insert(
            {k: previous.get(k) for k in ("product_id", "product_formulation_id", "analysis_type", "input_text", "result", "overall_status", "confidence_level", "data_source")}
        ).execute()
