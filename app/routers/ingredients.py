"""Ingredient catalog endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client

from app.database import get_supabase

router = APIRouter()


@router.get("")
async def list_ingredients(supabase: Client = Depends(get_supabase)) -> list[dict]:
    """Return all known ingredients ordered by name."""

    response = supabase.table("ingredients").select("*").order("name").execute()
    return response.data or []


# ── Madhab rulings (admins) ──────────────────────────────────────────────
# Rulings are entered by a person, with the scholar or body cited. Changing a
# ruling keeps the old one on file (superseded), so there is a history.

from datetime import datetime, timezone  # noqa: E402
from typing import Literal  # noqa: E402

from pydantic import BaseModel, Field  # noqa: E402

from app.database import get_current_user, is_admin  # noqa: E402
from app.services.rulings import SCHOOLS  # noqa: E402


def _require_admin(user: dict, supabase: Client) -> None:
    if not is_admin(user["id"], supabase):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")


def _refresh_rulings_cache(supabase: Client) -> None:
    from app.routers.products import rulings_index

    try:
        rulings_index(supabase, fresh=True)
    except Exception:
        pass


class IngredientCreate(BaseModel):
    """A catalogue entry. The general status and where it comes from are entered by the admin."""

    name: str = Field(min_length=2, max_length=80)
    e_number: str | None = Field(default=None, max_length=8, pattern=r"^[Ee]\d{3,4}[a-z]?$")
    common_aliases: list[str] = Field(default_factory=list, max_length=12)
    default_status: Literal["halal", "haram", "uncertain"]
    ruling_source: str = Field(min_length=3, max_length=300)
    ruling_notes: str | None = Field(default=None, max_length=1000)


class RulingCreate(BaseModel):
    madhab: Literal["hanafi", "shafii", "maliki", "hanbali"]
    status: Literal["halal", "haram", "uncertain"]
    reasoning: str = Field(min_length=10, max_length=2000)
    source: str = Field(min_length=3, max_length=300)
    source_url: str | None = Field(default=None, max_length=500, pattern=r"^https?://")


@router.get("/admin/rulings")
async def admin_rulings(user: dict = Depends(get_current_user), supabase: Client = Depends(get_supabase)) -> dict:
    """Every catalogued ingredient with its current ruling per school."""

    _require_admin(user, supabase)
    ingredients = supabase.table("ingredients").select("id,name,e_number,common_aliases,default_status,ruling_source,ruling_notes").order("name").execute().data or []
    rulings = (
        supabase.table("ingredient_rulings").select("id,ingredient_id,madhab,status,reasoning,source,source_url,effective_from")
        .is_("superseded_at", "null").execute().data
        or []
    )
    by_ingredient: dict[str, dict] = {}
    for r in rulings:
        by_ingredient.setdefault(r["ingredient_id"], {})[r["madhab"]] = r
    for ing in ingredients:
        ing["rulings"] = by_ingredient.get(ing["id"], {})
    return {"schools": list(SCHOOLS), "ingredients": ingredients}


@router.post("/admin")
async def add_ingredient(payload: IngredientCreate, user: dict = Depends(get_current_user), supabase: Client = Depends(get_supabase)) -> dict:
    """Add an ingredient to the catalogue (needed before a ruling can be attached to it)."""

    _require_admin(user, supabase)
    name = " ".join(payload.name.split())
    if supabase.table("ingredients").select("id").ilike("name", name).limit(1).execute().data:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That ingredient is already in the catalogue.")
    row = {
        "name": name,
        "e_number": payload.e_number.upper() if payload.e_number else None,
        "common_aliases": [" ".join(a.split()) for a in payload.common_aliases if a.strip()],
        "default_status": payload.default_status,
        "ruling_source": payload.ruling_source.strip(),
        "ruling_notes": (payload.ruling_notes or "").strip() or None,
    }
    saved = supabase.table("ingredients").insert(row).execute().data
    _refresh_rulings_cache(supabase)
    return saved[0] if saved else row


@router.post("/{ingredient_id}/rulings")
async def set_ruling(
    ingredient_id: str,
    payload: RulingCreate,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Record a school's ruling on an ingredient. Replaces the current one, which stays on file as superseded."""

    _require_admin(user, supabase)
    if not supabase.table("ingredients").select("id").eq("id", ingredient_id).limit(1).execute().data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ingredient not found.")
    now = datetime.now(timezone.utc).isoformat()
    previous = (
        supabase.table("ingredient_rulings").select("id").eq("ingredient_id", ingredient_id).eq("madhab", payload.madhab)
        .is_("superseded_at", "null").limit(1).execute().data
    )
    if previous:
        supabase.table("ingredient_rulings").update({"superseded_at": now}).eq("id", previous[0]["id"]).execute()
    saved = supabase.table("ingredient_rulings").insert(
        {
            "ingredient_id": ingredient_id,
            "madhab": payload.madhab,
            "status": payload.status,
            "reasoning": payload.reasoning.strip(),
            "source": payload.source.strip(),
            "source_url": payload.source_url,
            "created_by": user["id"],
        }
    ).execute().data
    if previous and saved:
        supabase.table("ingredient_rulings").update({"superseded_by": saved[0]["id"]}).eq("id", previous[0]["id"]).execute()
    _refresh_rulings_cache(supabase)
    return saved[0] if saved else {"saved": True}


@router.post("/rulings/{ruling_id}/withdraw")
async def withdraw_ruling(ruling_id: str, user: dict = Depends(get_current_user), supabase: Client = Depends(get_supabase)) -> dict:
    """Stop applying a ruling (it stays on file)."""

    _require_admin(user, supabase)
    supabase.table("ingredient_rulings").update({"superseded_at": datetime.now(timezone.utc).isoformat()}).eq("id", ruling_id).is_("superseded_at", "null").execute()
    _refresh_rulings_cache(supabase)
    return {"withdrawn": True}


@router.get("/{ingredient_id}")
async def get_ingredient(
    ingredient_id: str,
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Return one ingredient by ID."""

    response = (
        supabase.table("ingredients")
        .select("*")
        .eq("id", ingredient_id)
        .limit(1)
        .execute()
    )
    if not response.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ingredient not found.",
        )
    return response.data[0]
