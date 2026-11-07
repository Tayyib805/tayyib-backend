"""Community refute and challenge endpoints."""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from supabase import Client

from app.database import get_current_user, get_supabase
from app.services.claude_analysis import analyze_ingredients
from app.services.trust import vote_counts, why_not_counted

router = APIRouter()
optional_bearer_scheme = HTTPBearer(auto_error=False)

HalalStatus = Literal["halal", "haram", "uncertain"]
RefuteStatus = Literal["accepted", "rejected", "escalated_to_scholar"]
RefuteType = Literal["ingredient", "ruling", "manufacturer_confirmed"]
VoteType = Literal["support", "oppose"]


class CreateRefuteRequest(BaseModel):
    """Payload for challenging a product ruling."""

    ingredient_name: str | None = None
    challenged_status: HalalStatus
    proposed_status: HalalStatus
    reasoning: str = Field(min_length=20)
    evidence_url: str | None = None
    refute_type: RefuteType = "ingredient"


class VoteRequest(BaseModel):
    """Payload for supporting or opposing a refute."""

    vote_type: VoteType


class ResolveRefuteRequest(BaseModel):
    """Payload for resolving a refute from the admin queue."""

    status: RefuteStatus
    review_notes: str


def _now() -> str:
    """Return the current UTC timestamp as an ISO string."""

    return datetime.now(UTC).isoformat()


async def _get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(optional_bearer_scheme),
    supabase: Client = Depends(get_supabase),
) -> dict | None:
    """Return the current user when an Authorization header is present."""

    if credentials is None:
        return None

    try:
        response = supabase.auth.get_user(credentials.credentials)
        user = response.user
    except Exception:
        return None

    if user is None:
        return None
    return {"id": user.id, "email": user.email}


def _fetch_product(supabase: Client, product_id: str) -> dict | None:
    """Fetch a product by id."""

    response = (
        supabase.table("products")
        .select("*")
        .eq("id", product_id)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def _fetch_refute(supabase: Client, refute_id: str) -> dict | None:
    """Fetch a refute by id."""

    response = (
        supabase.table("product_refutes")
        .select("*")
        .eq("id", refute_id)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def _require_product(supabase: Client, product_id: str) -> dict:
    """Return a product or raise 404."""

    product = _fetch_product(supabase, product_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")
    return product


def _require_refute(supabase: Client, refute_id: str) -> dict:
    """Return a refute or raise 404."""

    refute = _fetch_refute(supabase, refute_id)
    if not refute:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Refute not found.")
    return refute


async def _require_admin(user: dict, supabase: Client) -> dict:
    """Ensure the current user is an administrator."""

    response = (
        supabase.table("user_profiles")
        .select("id,is_admin")
        .eq("id", user["id"])
        .limit(1)
        .execute()
    )
    profile = response.data[0] if response.data else None
    if not profile or not profile.get("is_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )
    return profile


def _latest_formulation_text(supabase: Client, product_id: str) -> str:
    """Return the latest formulation ingredients for a product, if any."""

    response = (
        supabase.table("product_formulations")
        .select("ingredients_text")
        .eq("product_id", product_id)
        .order("effective_from", desc=True)
        .limit(1)
        .execute()
    )
    if response.data:
        return response.data[0].get("ingredients_text") or ""
    return ""


def _update_product_challenge_counts(supabase: Client, product: dict) -> None:
    """Mark a product as challenged and increment challenge_count."""

    challenge_count = int(product.get("challenge_count") or 0) + 1
    supabase.table("products").update(
        {
            "has_open_challenges": True,
            "challenge_count": challenge_count,
            "updated_at": _now(),
        }
    ).eq("id", product["id"]).execute()


def _apply_accepted_refute(supabase: Client, refute: dict) -> None:
    """Apply an accepted refute to product and linked ingredient status."""

    proposed_status = refute.get("proposed_status")
    product_id = refute.get("product_id")
    if proposed_status not in {"halal", "haram", "uncertain"} or not product_id:
        return

    supabase.table("products").update(
        {"overall_status": proposed_status, "updated_at": _now()}
    ).eq("id", product_id).execute()

    ingredient_name = (refute.get("ingredient_name") or "").strip().lower()
    if not ingredient_name:
        return

    links = (
        supabase.table("product_ingredients")
        .select("id,ingredient_id,ingredients(name)")
        .eq("product_id", product_id)
        .execute()
    )
    for link in links.data or []:
        ingredient = link.get("ingredients") or {}
        if (ingredient.get("name") or "").strip().lower() != ingredient_name:
            continue
        supabase.table("ingredients").update({"default_status": proposed_status}).eq(
            "id",
            link["ingredient_id"],
        ).execute()
        supabase.table("product_ingredients").update(
            {
                "variant_risk": proposed_status == "uncertain",
                "notes": refute.get("review_notes") or refute.get("reasoning"),
            }
        ).eq("id", link["id"]).execute()


REVIEW_AT = 10
REANALYZE_AT = 25


async def _reanalyze_for_refute(
    supabase: Client,
    refute: dict,
    support_votes: int,
    oppose_votes: int = 0,
) -> tuple[str, str | None]:
    """Run threshold re-analysis; return the next status and a note for the reviewer.

    Re-analysis never changes the product by itself. The challenge text goes
    into the prompt, so a crafted challenge could steer it; an admin or scholar
    accepts or rejects from the review queue instead.
    """

    net = support_votes - oppose_votes
    current = refute.get("status") or "open"
    if current in {"accepted", "rejected", "escalated_to_scholar"}:
        return current, None
    if net < REANALYZE_AT:
        if net >= REVIEW_AT and current == "open":
            return "under_review", None
        return current, None

    product = _require_product(supabase, refute["product_id"])
    ingredients_text = product.get("ingredients_text") or _latest_formulation_text(
        supabase,
        product["id"],
    )
    analysis_input = f"{ingredients_text}\n\nCommunity challenge: {refute['reasoning']}"
    known = supabase.table("ingredients").select("name,e_number,default_status").execute()
    try:
        analysis = await analyze_ingredients(analysis_input, known.data or [])
        analysis_status = analysis.get("overall_status") or "uncertain"
        note = (
            f"Reached {net} net community votes. Re-analysis with the challenge suggests "
            f"{analysis_status} (confidence {analysis.get('confidence', 'low')}); the challenger "
            f"proposed {refute.get('proposed_status')}. Product unchanged until reviewed."
        )
    except Exception:
        note = f"Reached {net} net community votes. Re-analysis failed; review manually."
    return "escalated_to_scholar", note


@router.post("/products/{product_id}")
async def create_product_refute(
    product_id: str,
    payload: CreateRefuteRequest,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Create a community refute for a product."""

    product = _require_product(supabase, product_id)
    existing = (
        supabase.table("product_refutes")
        .select("id")
        .eq("product_id", product_id)
        .eq("user_id", user["id"])
        .limit(1)
        .execute()
    )
    if existing.data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User has already submitted a refute for this product.",
        )

    initial_status = "open"
    if payload.refute_type in {"ruling", "manufacturer_confirmed"}:
        initial_status = "escalated_to_scholar"

    created = (
        supabase.table("product_refutes")
        .insert(
            {
                "product_id": product_id,
                "user_id": user["id"],
                "ingredient_name": payload.ingredient_name,
                "challenged_status": payload.challenged_status,
                "proposed_status": payload.proposed_status,
                "reasoning": payload.reasoning,
                "evidence_url": payload.evidence_url,
                "refute_type": payload.refute_type,
                "status": initial_status,
            }
        )
        .execute()
    )
    if not created.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create refute.",
        )

    _update_product_challenge_counts(supabase, product)
    return created.data[0]


@router.post("/{refute_id}/vote")
async def vote_on_refute(
    refute_id: str,
    payload: VoteRequest,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Vote in support of or opposition to a refute."""

    refute = _require_refute(supabase, refute_id)
    if refute.get("user_id") == user["id"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Refute submitter cannot vote on their own refute.",
        )

    try:
        supabase.table("refute_votes").insert(
            {
                "refute_id": refute_id,
                "user_id": user["id"],
                "vote_type": payload.vote_type,
            }
        ).execute()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User has already voted on this refute.",
        ) from exc

    support_votes = int(refute.get("support_votes") or 0)
    oppose_votes = int(refute.get("oppose_votes") or 0)
    counted = vote_counts(supabase, user)
    if not counted:
        return {
            "refute_id": refute_id,
            "support_votes": support_votes,
            "oppose_votes": oppose_votes,
            "status": refute.get("status") or "open",
            "counted": False,
            "message": why_not_counted(),
        }
    if payload.vote_type == "support":
        support_votes += 1
    else:
        oppose_votes += 1

    next_status, note = await _reanalyze_for_refute(supabase, refute, support_votes, oppose_votes)
    updates = {
        "support_votes": support_votes,
        "oppose_votes": oppose_votes,
        "status": next_status,
        "updated_at": _now(),
    }
    if note:
        updates["review_notes"] = note
    updated = (
        supabase.table("product_refutes")
        .update(updates)
        .eq("id", refute_id)
        .execute()
    )
    return {
        "refute_id": refute_id,
        "support_votes": support_votes,
        "oppose_votes": oppose_votes,
        "status": next_status,
        "counted": True,
    }


@router.get("/products/{product_id}")
async def get_product_refutes(
    product_id: str,
    user: dict | None = Depends(_get_optional_user),
    supabase: Client = Depends(get_supabase),
) -> list[dict]:
    """Return all refutes for a product."""

    response = (
        supabase.table("product_refutes")
        .select("*")
        .eq("product_id", product_id)
        .order("support_votes", desc=True)
        .execute()
    )
    refutes = response.data or []
    for refute in refutes:
        # Public endpoint: never reveal who filed a refute or who reviewed it.
        refute.pop("user_id", None)
        refute.pop("reviewed_by", None)
    refutes.sort(key=lambda item: (0 if item.get("status") == "open" else 1, -int(item.get("support_votes") or 0)))

    if not user or not refutes:
        return refutes

    refute_ids = [refute["id"] for refute in refutes]
    votes = (
        supabase.table("refute_votes")
        .select("refute_id,vote_type")
        .eq("user_id", user["id"])
        .in_("refute_id", refute_ids)
        .execute()
    )
    votes_by_refute = {vote["refute_id"]: vote["vote_type"] for vote in votes.data or []}
    for refute in refutes:
        vote_type = votes_by_refute.get(refute["id"])
        refute["has_voted"] = vote_type is not None
        refute["current_user_vote"] = vote_type
    return refutes


@router.get("/admin/queue")
async def get_admin_refute_queue(
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> list[dict]:
    """Return refutes waiting for admin or scholar review."""

    await _require_admin(user, supabase)
    response = (
        supabase.table("product_refutes")
        .select("*, products(name,brand,barcode,overall_status)")
        .in_("status", ["under_review", "escalated_to_scholar"])
        .order("support_votes", desc=True)
        .execute()
    )
    return response.data or []


@router.post("/{refute_id}/resolve")
async def resolve_refute(
    refute_id: str,
    payload: ResolveRefuteRequest,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Resolve a refute from the admin queue."""

    await _require_admin(user, supabase)
    refute = _require_refute(supabase, refute_id)
    updates = {
        "status": payload.status,
        "review_notes": payload.review_notes,
        "updated_at": _now(),
    }
    updated = (
        supabase.table("product_refutes")
        .update(updates)
        .eq("id", refute_id)
        .execute()
    )
    if not updated.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to resolve refute.",
        )

    resolved = updated.data[0]
    if payload.status == "accepted":
        _apply_accepted_refute(supabase, {**refute, **resolved})
    return resolved
