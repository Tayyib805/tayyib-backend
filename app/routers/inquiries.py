"""Manufacturer inquiry workflow endpoints."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from supabase import Client

from app.database import get_current_user, get_supabase
from app.ratelimit import rate_limit
from app.services.claude_analysis import analyze_ingredients
from app.services.email_service import send_inquiry_email
from app.services.inquiry_generator import generate_inquiry_email
from app.services.openfoodfacts import extract_domain, lookup_barcode

router = APIRouter()


class SetManufacturerEmailRequest(BaseModel):
    """Payload for assigning a manufacturer contact email."""

    manufacturer_id: str
    email: str


class ManufacturerResponseRequest(BaseModel):
    """Payload for logging a manufacturer response."""

    response_body: str
    resolved: bool
    resolution_notes: str | None = None


def _now() -> str:
    """Return the current UTC timestamp as an ISO string."""

    return datetime.now(UTC).isoformat()


def _fetch_product(supabase: Client, product_id: str) -> dict | None:
    """Fetch a product with any linked ingredient data."""

    response = (
        supabase.table("products")
        .select("*, product_ingredients(*, ingredients(*))")
        .eq("id", product_id)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def _flagged_from_product(product: dict) -> list[dict]:
    """Extract flagged ingredients from product data if available."""

    analysis = product.get("analysis") or {}
    if isinstance(analysis, dict) and analysis.get("flagged_ingredients"):
        return analysis["flagged_ingredients"]

    if product.get("flagged_ingredients"):
        return product["flagged_ingredients"]

    flagged = []
    for link in product.get("product_ingredients") or []:
        ingredient = link.get("ingredients") or {}
        status_value = ingredient.get("default_status")
        if status_value and status_value != "halal":
            flagged.append(
                {
                    "name": ingredient.get("name", "Unknown ingredient"),
                    "status": status_value,
                    "reason": link.get("notes")
                    or ingredient.get("education_card_content")
                    or "Ingredient requires manufacturer clarification.",
                    "e_number": ingredient.get("e_number"),
                }
            )
        elif link.get("variant_risk"):
            flagged.append(
                {
                    "name": ingredient.get("name", "Unknown ingredient"),
                    "status": "uncertain",
                    "reason": link.get("notes") or "Ingredient has source-dependent halal status.",
                    "e_number": ingredient.get("e_number"),
                }
            )
    return flagged


async def _resolve_flagged_ingredients(supabase: Client, product: dict) -> list[dict]:
    """Resolve flagged ingredients from stored data or fresh barcode analysis."""

    flagged = _flagged_from_product(product)
    if flagged or not product.get("barcode"):
        return flagged

    off_product = await lookup_barcode(product["barcode"])
    if not off_product.get("found"):
        return []

    known = supabase.table("ingredients").select("name,e_number,default_status").execute()
    analysis = await analyze_ingredients(
        off_product.get("ingredients_text", ""),
        known.data or [],
    )
    return analysis.get("flagged_ingredients") or []


def _get_existing_inquiry(supabase: Client, product_id: str) -> dict | None:
    """Return an existing non-failed inquiry for a product, if present."""

    response = (
        supabase.table("manufacturer_inquiries")
        .select("*, manufacturers(*)")
        .eq("product_id", product_id)
        .neq("send_status", "failed")
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def _get_manufacturer(supabase: Client, brand_name: str) -> dict | None:
    """Fetch an existing manufacturer by brand name."""

    response = (
        supabase.table("manufacturers")
        .select("*")
        .eq("brand_name", brand_name)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


async def _create_manufacturer_from_off(
    supabase: Client,
    product: dict,
    brand_name: str,
) -> dict:
    """Create a manufacturer record using Open Food Facts contact data."""

    off_product = await lookup_barcode(product.get("barcode", ""))
    website = off_product.get("website") if off_product.get("found") else None
    contact_email = off_product.get("contact_email") if off_product.get("found") else None
    manufacturer_payload = {
        "brand_name": brand_name,
        "domain": extract_domain(website) if website else None,
        "contact_email": contact_email,
        "email_verified": bool(contact_email),
        "email_source": "open_food_facts" if contact_email else None,
    }
    created = supabase.table("manufacturers").insert(manufacturer_payload).execute()
    if not created.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create manufacturer.",
        )
    return created.data[0]


async def _send_and_update_inquiry(
    supabase: Client,
    inquiry: dict,
    manufacturer: dict,
) -> dict:
    """Send an inquiry email and update its send status."""

    result = await send_inquiry_email(
        to_email=manufacturer["contact_email"],
        brand_name=inquiry["brand_name"],
        product_name=inquiry["product_name"],
        email_body=inquiry["email_body"],
        inquiry_id=inquiry["id"],
    )
    updates = {
        "recipient_email": manufacturer["contact_email"],
        "send_status": "sent" if result["success"] else "failed",
        "sent_at": _now() if result["success"] else None,
        "updated_at": _now(),
    }
    updated = (
        supabase.table("manufacturer_inquiries")
        .update(updates)
        .eq("id", inquiry["id"])
        .execute()
    )
    return updated.data[0] if updated.data else {**inquiry, **updates}


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


@router.post(
    "/trigger/{product_id}",
    dependencies=[Depends(rate_limit("inquiry_trigger", 5, 3_600))],
)
async def trigger_inquiry(
    product_id: str,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Draft a manufacturer inquiry for a product.

    Drafts are never sent automatically. Every email waits in the admin queue
    until an admin approves it with POST /inquiries/admin/{inquiry_id}/approve.
    """

    del user

    product = _fetch_product(supabase, product_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")

    existing = _get_existing_inquiry(supabase, product_id)
    if existing:
        return {"message": "Inquiry already exists", "inquiry": existing}

    flagged_ingredients = await _resolve_flagged_ingredients(supabase, product)
    if not flagged_ingredients:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No uncertain ingredients to inquire about.",
        )

    brand_name = product.get("brand") or "Unknown brand"
    manufacturer = _get_manufacturer(supabase, brand_name)
    if not manufacturer:
        manufacturer = await _create_manufacturer_from_off(supabase, product, brand_name)

    email_body = await generate_inquiry_email(
        product_name=product["name"],
        brand_name=brand_name,
        barcode=product.get("barcode") or "",
        flagged_ingredients=flagged_ingredients,
    )
    create_payload = {
        "product_id": product_id,
        "manufacturer_id": manufacturer["id"],
        "brand_name": brand_name,
        "product_name": product["name"],
        "barcode": product.get("barcode") or "",
        "flagged_ingredients": flagged_ingredients,
        "email_body": email_body,
        "recipient_email": manufacturer.get("contact_email"),
        "send_status": "pending_send" if manufacturer.get("contact_email") else "pending_email",
    }
    created = supabase.table("manufacturer_inquiries").insert(create_payload).execute()
    if not created.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create inquiry.",
        )

    if manufacturer.get("contact_email"):
        message = "Inquiry drafted and waiting for admin approval."
    else:
        message = f"Inquiry drafted - admin needs to add an email address for {brand_name}."
    return {"message": message, "inquiry_id": created.data[0]["id"]}


@router.get("/products/{product_id}")
async def get_product_inquiries(
    product_id: str,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> list[dict]:
    """Admin only - get all inquiries for a product, including drafts and contacts."""

    await _require_admin(user, supabase)
    response = (
        supabase.table("manufacturer_inquiries")
        .select("*, manufacturers(*)")
        .eq("product_id", product_id)
        .order("created_at", desc=True)
        .execute()
    )
    return response.data or []


@router.get("/admin/queue")
async def get_admin_queue(
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> list[dict]:
    """Admin only - inquiries waiting for an email address or for send approval."""

    await _require_admin(user, supabase)
    response = (
        supabase.table("manufacturer_inquiries")
        .select("*, manufacturers(*)")
        .in_("send_status", ["pending_email", "pending_send"])
        .order("created_at", desc=True)
        .execute()
    )
    return response.data or []


@router.post("/admin/set-email")
async def set_manufacturer_email(
    payload: SetManufacturerEmailRequest,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Admin sets a manufacturer email. Pending drafts move to awaiting approval."""

    await _require_admin(user, supabase)
    manufacturer_response = (
        supabase.table("manufacturers")
        .update(
            {
                "contact_email": str(payload.email),
                "email_source": "admin_manual",
                "email_verified": True,
                "updated_at": _now(),
            }
        )
        .eq("id", payload.manufacturer_id)
        .execute()
    )
    if not manufacturer_response.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Manufacturer not found.",
        )
    manufacturer = manufacturer_response.data[0]

    moved = (
        supabase.table("manufacturer_inquiries")
        .update(
            {
                "recipient_email": manufacturer["contact_email"],
                "send_status": "pending_send",
                "updated_at": _now(),
            }
        )
        .eq("manufacturer_id", payload.manufacturer_id)
        .eq("send_status", "pending_email")
        .execute()
    )

    return {
        "message": "Manufacturer email updated. Drafts are ready for your approval.",
        "inquiries_awaiting_approval": len(moved.data or []),
    }


@router.post("/admin/{inquiry_id}/approve")
async def approve_inquiry(
    inquiry_id: str,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Admin approves a drafted inquiry and sends it to the manufacturer."""

    await _require_admin(user, supabase)
    response = (
        supabase.table("manufacturer_inquiries")
        .select("*, manufacturers(*)")
        .eq("id", inquiry_id)
        .limit(1)
        .execute()
    )
    inquiry = response.data[0] if response.data else None
    if not inquiry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inquiry not found.")
    if inquiry.get("send_status") not in {"pending_send", "failed"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Inquiry is '{inquiry.get('send_status')}' and cannot be sent.",
        )
    manufacturer = inquiry.get("manufacturers") or {}
    if not manufacturer.get("contact_email"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Add a manufacturer email before approving.",
        )

    inquiry.pop("manufacturers", None)
    return await _send_and_update_inquiry(supabase, inquiry, manufacturer)


@router.post("/{inquiry_id}/response")
async def log_manufacturer_response(
    inquiry_id: str,
    payload: ManufacturerResponseRequest,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Admin only - log a manufacturer's response as evidence."""

    await _require_admin(user, supabase)

    updates = {
        "response_body": payload.response_body,
        "resolved": payload.resolved,
        "resolution_notes": payload.resolution_notes,
        "response_received_at": _now(),
        "send_status": "responded",
        "updated_at": _now(),
    }
    response = (
        supabase.table("manufacturer_inquiries")
        .update(updates)
        .eq("id", inquiry_id)
        .execute()
    )
    if not response.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Inquiry not found.",
        )

    inquiry = response.data[0]
    if payload.resolved and inquiry.get("product_id"):
        product = (
            supabase.table("products")
            .select("id,vote_count")
            .eq("id", inquiry["product_id"])
            .limit(1)
            .execute()
        )
        if product.data:
            vote_count = int(product.data[0].get("vote_count") or 0) + 1
            supabase.table("products").update({"vote_count": vote_count}).eq(
                "id", inquiry["product_id"]
            ).execute()

    return inquiry
