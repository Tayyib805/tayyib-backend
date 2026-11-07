"""Label spot-checks: does the label in the shop still match what we hold?

A person photographs the ingredients of a product we already have. If the
list matches, the stored formulation is marked verified. If it has
changed, the old formulation is closed, the new one is stored, and the
change goes to the admin review queue. For unreviewed (tier 3) products the
new verdict applies straight away, the same as any label photo; a reviewed
verdict (tier 1-2) is never replaced without an admin.
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
    _record_analysis,
    _save_flagged_ingredients,
)
from app.services.claude_analysis import analyze_ingredients, describe_error, extract_ingredients_from_image
from app.services.formulation_compare import compare
from app.services.points import DAILY_LABEL_AWARDS, LABEL_PHOTO, award_points, under_daily_cap
from app.services.push import notify, product_followers

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
    if under_daily_cap(supabase, "label_scans", user_id, DAILY_LABEL_AWARDS):
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
    stored = _fetch_latest_formulation(supabase, product_id, country)
    if not stored or not stored.get("ingredients_text"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We don't hold this product's ingredients yet.")

    try:
        extraction = await extract_ingredients_from_image(payload.image_base64, payload.media_type)
    except Exception as exc:
        log.error("Label reading failed: %s", describe_error(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="We couldn't read labels just now. Try again in a minute.",
        )
    label_text = (extraction or {}).get("ingredients_text") if isinstance(extraction, dict) else None
    diff = compare(stored["ingredients_text"], label_text or "")
    if not (extraction or {}).get("found") or not label_text or not diff["readable"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="We couldn't see an ingredients list. Photograph just the ingredients, close up and in good light.",
        )

    supabase.table("label_scans").insert(
        {
            "product_id": product_id,
            "user_id": user["id"],
            "extracted_ingredients": label_text,
            "extraction_confidence": _normalize_confidence(extraction.get("confidence")),
            "country_code": country,
            "verified": diff["matched"],
        }
    ).execute()
    points = _award(supabase, user["id"])

    if diff["matched"]:
        supabase.table("product_formulations").update({"last_verified_at": _now()}).eq("id", stored["id"]).execute()
        return {
            "matched": True,
            "points_awarded": points,
            "message": "Your label matches what we have. Thank you for checking.",
        }

    # The recipe looks different. Keep the old record, start a new one.
    now = _now()
    supabase.table("product_formulations").update({"effective_to": now}).eq("id", stored["id"]).execute()
    created = (
        supabase.table("product_formulations")
        .insert(
            {
                "product_id": product_id,
                "ingredients_text": label_text,
                "country_code": country,
                "data_source": "label_scan",
                "effective_from": now,
                "last_verified_at": now,
            }
        )
        .execute()
    ).data
    new_formulation = created[0] if created else None

    known = supabase.table("ingredients").select("name,e_number,default_status").execute()
    analysis = await analyze_ingredients(label_text, known.data or [])
    analysed = not analysis.get("failed")
    old_status = product.get("overall_status")
    new_status = analysis.get("overall_status") if analysed else None
    protected = int(product.get("verification_tier") or 3) < 3
    applied = analysed and not protected
    if applied:
        _apply_analysis(supabase, product_id, new_formulation, label_text, analysis)

    supabase.table("formulation_changes").insert(
        {
            "product_id": product_id,
            "country_code": country,
            "old_formulation_id": stored["id"],
            "new_formulation_id": (new_formulation or {}).get("id"),
            "user_id": user["id"],
            "added": diff["added"],
            "removed": diff["removed"],
            "old_status": old_status,
            "new_status": new_status,
            "analysis": {k: analysis.get(k) for k in ("overall_status", "confidence", "flagged_ingredients")} if analysed else None,
            "applied": applied,
        }
    ).execute()

    if applied and new_status != old_status:
        _tell_followers(supabase, product, new_status, exclude={user["id"]})

    result = {
        "matched": False,
        "added": diff["added"],
        "removed": diff["removed"],
        "old_status": old_status,
        "new_status": new_status,
        "applied": applied,
        "points_awarded": points,
    }
    if applied:
        enriched = _fetch_product_with_ingredients(supabase, "id", product_id) or product
        result["verdict"] = _build_scan_response(supabase, enriched, country)
        result["message"] = "We've updated the verdict to match your label. Thank you for checking."
    elif protected:
        result["message"] = "A person reviewed this product before, so we'll check the change before updating its verdict. Thank you."
    else:
        result["message"] = "We've saved the new ingredients and will check them shortly. Thank you."
    return result


def _apply_analysis(supabase: Client, product_id: str, formulation: dict | None, text: str, analysis: dict) -> None:
    supabase.table("products").update(
        {
            "overall_status": analysis.get("overall_status"),
            "confidence_level": _normalize_confidence(analysis.get("confidence")),
            "ingredients_text": text,
            "analyzed_at": _now(),
            "verification_tier": 3,
        }
    ).eq("id", product_id).execute()
    _save_flagged_ingredients(supabase, product_id, analysis.get("flagged_ingredients") or [], analysis.get("confidence"))
    _record_analysis(supabase, product_id, formulation, text, analysis, "label_scan")


WORD = {"halal": "halal", "haram": "haram", "uncertain": "unsure"}


def _tell_followers(supabase: Client, product: dict, new_status: str | None, exclude: set[str]) -> None:
    followers = product_followers(supabase, product["id"]) - exclude
    notify(
        supabase,
        followers,
        "A recipe changed",
        f"{product.get('name') or 'A product you follow'} has new ingredients and is now marked {WORD.get(new_status or '', 'unsure')}.",
        f"/?product={product['id']}",
        f"recipe-{product['id']}",
    )


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
        if not change.get("applied") and change.get("new_formulation_id"):
            # The verdict was for the old recipe; the new one gets its own.
            formulation = {"id": change["new_formulation_id"]}
            text = (
                supabase.table("product_formulations").select("ingredients_text").eq("id", change["new_formulation_id"]).limit(1).execute().data
                or [{}]
            )[0].get("ingredients_text") or ""
            analysis = change.get("analysis")
            if not analysis and text:
                known = supabase.table("ingredients").select("name,e_number,default_status").execute()
                analysis = await analyze_ingredients(text, known.data or [])
                if analysis.get("failed"):
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="We couldn't check the new ingredients just now. Try again in a minute.",
                    )
            if analysis and text:
                _apply_analysis(supabase, change["product_id"], formulation, text, analysis)
                if analysis.get("overall_status") != product.get("overall_status"):
                    _tell_followers(supabase, product, analysis.get("overall_status"), exclude=set())
    else:
        # The photo was wrong: drop the new record and reopen the old one.
        if change.get("new_formulation_id"):
            supabase.table("product_formulations").delete().eq("id", change["new_formulation_id"]).execute()
        if change.get("old_formulation_id"):
            supabase.table("product_formulations").update({"effective_to": None}).eq("id", change["old_formulation_id"]).execute()
        if change.get("applied"):
            _restore_previous(supabase, change)

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
