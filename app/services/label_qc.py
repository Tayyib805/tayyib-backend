"""Quality control for ingredients read from label photos.

One photo is never enough to change what everyone sees. A list read from a
photo is stored unconfirmed and becomes trusted when:
  - CONFIRMATIONS_NEEDED different accounts photograph a matching label, or
  - an admin confirms it in the review queue, or
  - the manufacturer's official list matches it (or replaces it).

A brand-new product still gets a verdict from the first photo, so the
shopper has an answer, but it is shown as unconfirmed and kept out of
public lists until confirmed. A recipe change reported for a product we
already hold is never applied from one photo.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.services.formulation_compare import compare

if TYPE_CHECKING:
    from supabase import Client

CONFIRMATIONS_NEEDED = 3
STATUS_WORD = {"halal": "halal", "haram": "haram", "uncertain": "unsure"}
log = logging.getLogger("tayyib.labels")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def formulations(supabase: "Client", product_id: str, country: str) -> tuple[dict | None, dict | None]:
    """(current, pending) ingredient lists for a product in a country.

    current: the newest confirmed list, or for a product known only from
    photos, its newest unconfirmed list. pending: the newest unconfirmed list
    from a photo that is newer than the current confirmed one.
    """

    rows = (
        supabase.table("product_formulations")
        .select("*")
        .eq("product_id", product_id)
        .eq("country_code", country)
        .order("effective_from", desc=True)
        .limit(20)
        .execute()
    ).data or []
    confirmed = next((r for r in rows if r.get("confirmed_at") and not r.get("effective_to")), None)
    confirmed = confirmed or next((r for r in rows if r.get("confirmed_at")), None)
    unconfirmed = [r for r in rows if not r.get("confirmed_at") and r.get("ingredients_text")]
    if confirmed:
        newer = [r for r in unconfirmed if str(r.get("effective_from") or "") > str(confirmed.get("effective_from") or "")]
        return confirmed, (newer[0] if newer else None)
    if unconfirmed:
        return unconfirmed[0], unconfirmed[0]
    return None, None


def confirmations(supabase: "Client", formulation_id: str) -> int:
    """Different accounts whose photo matched this ingredient list."""

    rows = (
        supabase.table("label_scans").select("user_id").eq("formulation_id", formulation_id).eq("verified", True).execute()
    ).data or []
    return len({r["user_id"] for r in rows if r.get("user_id")})


def label_status(supabase: "Client", product: dict, country: str) -> dict | None:
    """What the app shows when ingredients still need confirming, else None."""

    current, pending = formulations(supabase, product["id"], country)
    if not pending:
        return None
    photos = confirmations(supabase, pending["id"])
    if current is pending:
        return {"state": "unconfirmed", "photos": photos, "needed": CONFIRMATIONS_NEEDED}
    change = (
        supabase.table("formulation_changes")
        .select("added,removed,new_status")
        .eq("new_formulation_id", pending["id"])
        .eq("status", "pending")
        .limit(1)
        .execute()
    ).data
    change = change[0] if change else {}
    return {
        "state": "change_reported",
        "photos": photos,
        "needed": CONFIRMATIONS_NEEDED,
        "added": change.get("added") or [],
        "removed": change.get("removed") or [],
        "new_status": change.get("new_status"),
    }


def record_photo(
    supabase: "Client",
    *,
    product_id: str,
    formulation_id: str | None,
    user_id: str,
    text: str,
    confidence: str,
    country: str,
    matched: bool,
) -> bool:
    """Store a label photo's reading. True if this account hadn't yet
    confirmed this list (only first confirmations earn points)."""

    first = True
    if formulation_id:
        prior = (
            supabase.table("label_scans")
            .select("id")
            .eq("formulation_id", formulation_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        ).data
        first = not prior
    supabase.table("label_scans").insert(
        {
            "product_id": product_id,
            "user_id": user_id,
            "formulation_id": formulation_id,
            "extracted_ingredients": text,
            "extraction_confidence": confidence if confidence in {"high", "medium", "low"} else "low",
            "country_code": country,
            "verified": matched,
        }
    ).execute()
    return first


async def confirm_formulation(supabase: "Client", product: dict, formulation: dict, by: str) -> dict:
    """Make an ingredient list the trusted one and update the verdict.

    by: "photos" (enough matching photos), "admin" or "manufacturer".
    Enough photos never override a person-reviewed verdict (tier 1-2); an
    admin or the manufacturer's list does.
    """

    from app.routers.products import _fetch_product_with_ingredients, _normalize_confidence, _record_analysis, _save_flagged_ingredients
    from app.services.claude_analysis import analyze_ingredients
    from app.services.push import notify, product_followers

    protected = int(product.get("verification_tier") or 3) < 3
    if by == "photos" and protected:
        return {"confirmed": False, "waiting_for_admin": True}

    now = _now()
    supabase.table("product_formulations").update({"confirmed_at": now, "last_verified_at": now}).eq("id", formulation["id"]).execute()
    # Close the list it replaces.
    older = (
        supabase.table("product_formulations")
        .select("id,effective_from")
        .eq("product_id", product["id"])
        .eq("country_code", formulation.get("country_code") or "US")
        .is_("effective_to", "null")
        .neq("id", formulation["id"])
        .execute()
    ).data or []
    for row in older:
        if str(row.get("effective_from") or "") <= str(formulation.get("effective_from") or now):
            supabase.table("product_formulations").update({"effective_to": now}).eq("id", row["id"]).execute()

    change = (
        supabase.table("formulation_changes")
        .select("*")
        .eq("new_formulation_id", formulation["id"])
        .eq("status", "pending")
        .limit(1)
        .execute()
    ).data
    change = change[0] if change else None
    already_applied = bool(change and change.get("applied"))
    old_status = product.get("overall_status")
    new_status = old_status
    result = {"confirmed": True, "applied": already_applied}

    if not already_applied:
        analysis = (change or {}).get("analysis")
        text = formulation.get("ingredients_text") or ""
        if not analysis and text:
            known = supabase.table("ingredients").select("name,e_number,default_status").execute()
            analysis = await analyze_ingredients(text, known.data or [])
            if analysis.get("failed"):
                analysis = None
                result["verdict_pending"] = True
        if analysis and text:
            supabase.table("products").update(
                {
                    "overall_status": analysis.get("overall_status"),
                    "confidence_level": _normalize_confidence(analysis.get("confidence")),
                    "ingredients_text": text,
                    "analyzed_at": now,
                    "verification_tier": 3,
                }
            ).eq("id", product["id"]).execute()
            _save_flagged_ingredients(supabase, product["id"], analysis.get("flagged_ingredients") or [], analysis.get("confidence"))
            _record_analysis(supabase, product["id"], formulation, text, analysis, formulation.get("data_source") or "label_scan")
            new_status = analysis.get("overall_status")
            result["applied"] = True

    if change:
        supabase.table("formulation_changes").update(
            {
                "status": "confirmed",
                "applied": result["applied"],
                "reviewed_at": now,
                "review_notes": change.get("review_notes") or f"Confirmed by {by}.",
            }
        ).eq("id", change["id"]).execute()

    if result["applied"] and new_status != old_status and change and change.get("old_formulation_id"):
        notify(
            supabase,
            product_followers(supabase, product["id"]),
            "A recipe changed",
            f"{product.get('name') or 'A product you follow'} has new ingredients and is now marked "
            f"{STATUS_WORD.get(new_status or '', 'unsure')}.",
            f"/?product={product['id']}",
            f"recipe-{product['id']}",
        )
    result["product"] = _fetch_product_with_ingredients(supabase, "id", product["id"]) or product
    return result


async def process_label(
    supabase: "Client",
    *,
    product: dict,
    country: str,
    user_id: str,
    text: str,
    confidence: str,
) -> dict:
    """Handle a label photo for a product we already hold ingredients for."""

    from app.routers.products import _normalize_confidence
    from app.services.claude_analysis import analyze_ingredients

    current, pending = formulations(supabase, product["id"], country)
    candidates = [f for f in (pending, current) if f]
    seen = set()
    candidates = [f for f in candidates if not (f["id"] in seen or seen.add(f["id"]))]

    for f in candidates:
        diff = compare(f.get("ingredients_text") or "", text)
        if not diff["readable"]:
            return {"unreadable": True}
        if diff["matched"]:
            first = record_photo(
                supabase, product_id=product["id"], formulation_id=f["id"], user_id=user_id,
                text=text, confidence=confidence, country=country, matched=True,
            )
            if f.get("confirmed_at"):
                supabase.table("product_formulations").update({"last_verified_at": _now()}).eq("id", f["id"]).execute()
                return {"matched": True, "matched_pending": False, "first_time": first,
                        "disputes_change": bool(pending and pending["id"] != f["id"])}
            photos = confirmations(supabase, f["id"])
            outcome = {"matched": True, "matched_pending": True, "first_time": first, "photos": photos, "needed": CONFIRMATIONS_NEEDED}
            if photos >= CONFIRMATIONS_NEEDED:
                confirmed = await confirm_formulation(supabase, product, f, by="photos")
                outcome.update({k: v for k, v in confirmed.items() if k != "product"})
                outcome["product"] = confirmed.get("product")
            return outcome

    # Different from every list we hold: store it unconfirmed, for review.
    base = current or {}
    diff = compare(base.get("ingredients_text") or "", text)
    now = _now()
    created = (
        supabase.table("product_formulations")
        .insert({
            "product_id": product["id"], "ingredients_text": text, "country_code": country,
            "data_source": "label_scan", "effective_from": now,
        })
        .execute()
    ).data
    new_f = created[0] if created else None
    first = record_photo(
        supabase, product_id=product["id"], formulation_id=(new_f or {}).get("id"), user_id=user_id,
        text=text, confidence=confidence, country=country, matched=True,
    )
    known = supabase.table("ingredients").select("name,e_number,default_status").execute()
    analysis = await analyze_ingredients(text, known.data or [])
    analysed = not analysis.get("failed")
    supabase.table("formulation_changes").insert({
        "product_id": product["id"], "country_code": country,
        "old_formulation_id": base.get("id"), "new_formulation_id": (new_f or {}).get("id"),
        "user_id": user_id, "added": diff["added"], "removed": diff["removed"],
        "old_status": product.get("overall_status"),
        "new_status": analysis.get("overall_status") if analysed else None,
        "analysis": {k: analysis.get(k) for k in ("overall_status", "confidence", "flagged_ingredients")} if analysed else None,
        "applied": False,
    }).execute()
    await request_official_list(supabase, product, analysis.get("flagged_ingredients") if analysed else [])
    return {
        "matched": False, "first_time": first, "added": diff["added"], "removed": diff["removed"],
        "new_status": analysis.get("overall_status") if analysed else None,
        "photos": 1, "needed": CONFIRMATIONS_NEEDED,
        "confidence": _normalize_confidence(analysis.get("confidence")) if analysed else None,
    }


async def request_official_list(supabase: "Client", product: dict, flagged: list | None) -> None:
    """Draft (never send) an email asking the manufacturer for the official
    ingredient list. It waits in the admin queue like every other email."""

    try:
        from app.routers.inquiries import draft_ingredient_request

        await draft_ingredient_request(supabase, product, flagged or [])
    except Exception as exc:  # a missing draft must never fail the photo
        log.warning("Couldn't draft the ingredient-list request: %s", exc)


async def apply_official_list(supabase: "Client", product_id: str, country: str, text: str) -> dict:
    """The manufacturer sent their ingredient list: it confirms a list we
    hold if they match, otherwise it becomes the trusted list."""

    from app.routers.products import _fetch_product_with_ingredients, _normalize_country_code

    product = _fetch_product_with_ingredients(supabase, "id", product_id)
    if not product:
        return {"result": "product_missing"}
    country = _normalize_country_code(country)
    current, pending = formulations(supabase, product_id, country)
    for f in [x for x in (pending, current) if x]:
        if compare(f.get("ingredients_text") or "", text)["matched"]:
            if f.get("confirmed_at"):
                supabase.table("product_formulations").update({"last_verified_at": _now()}).eq("id", f["id"]).execute()
                return {"result": "matches_current"}
            r = await confirm_formulation(supabase, product, f, by="manufacturer")
            return {"result": "confirmed_photos", "applied": r.get("applied", False)}

    diff = compare((current or {}).get("ingredients_text") or "", text)
    created = (
        supabase.table("product_formulations")
        .insert({
            "product_id": product_id, "ingredients_text": text, "country_code": country,
            "data_source": "admin_entry", "effective_from": _now(),
        })
        .execute()
    ).data
    if not created:
        return {"result": "not_saved"}
    # Photo readings it supersedes are closed as replaced.
    supabase.table("formulation_changes").update(
        {"status": "reverted", "reviewed_at": _now(), "review_notes": "Replaced by the manufacturer's official list."}
    ).eq("product_id", product_id).eq("status", "pending").execute()
    r = await confirm_formulation(supabase, product, created[0], by="manufacturer")
    return {
        "result": "replaced",
        "applied": r.get("applied", False),
        "verdict_pending": r.get("verdict_pending", False),
        "added": diff["added"],
        "removed": diff["removed"],
    }


def outcome_message(o: dict) -> str:
    """What to tell the person who sent the photo."""

    if o.get("matched") and not o.get("matched_pending"):
        if o.get("disputes_change"):
            return "Your label matches the ingredients we had before, so it counts against the reported change. Thank you."
        return "Your label matches what we have. Thank you for checking."
    if o.get("matched"):
        if not o.get("first_time"):
            return "You've already confirmed this list. We need photos from other shoppers too."
        if o.get("confirmed") and o.get("applied"):
            return "That's enough matching photos: the ingredients are confirmed and the verdict is updated. Thank you."
        if o.get("confirmed"):
            return "That's enough matching photos: the ingredients are confirmed. Thank you."
        if o.get("waiting_for_admin"):
            return "That's enough matching photos. A person reviewed this product before, so we'll check before changing its verdict."
        left = max(int(o.get("needed") or 0) - int(o.get("photos") or 0), 0)
        return f"Your photo matches. {left} more from other shoppers and it's confirmed."
    return (
        "This label is different from what we have. We've saved it, and it needs "
        f"{int(o.get('needed') or 3) - 1} more matching photos from other shoppers, or our review, before anything changes."
    )
