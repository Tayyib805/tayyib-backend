"""Ingredient request and response models."""

from pydantic import BaseModel


class IngredientResponse(BaseModel):
    """Serializable ingredient response."""

    id: str | None = None
    name: str
    e_number: str | None = None
    default_status: str = "uncertain"
    has_variants: bool = False
    education_card_content: str | None = None
