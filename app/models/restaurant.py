"""Restaurant request and response models."""

from pydantic import BaseModel, Field


class RestaurantCreate(BaseModel):
    """Payload for suggesting a restaurant. It stays unverified until reviewed."""

    name: str = Field(min_length=2, max_length=120)
    address: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=500)
    lat: float | None = None
    lng: float | None = None
    cuisine_type: str | None = None
    phone: str | None = None
    website: str | None = None
    halal_status: str = "unverified"
    certification_doc_url: str | None = None
    alcohol_on_premises: bool = False
    prayer_space: bool = False


class RestaurantReviewCreate(BaseModel):
    """Payload for creating a restaurant review."""

    rating: int = Field(ge=1, le=5)
    halal_confidence: int = Field(ge=1, le=5)
    body: str | None = None
