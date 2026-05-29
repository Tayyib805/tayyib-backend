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
