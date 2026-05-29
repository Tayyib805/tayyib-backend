"""Product request and response models."""

from pydantic import BaseModel


class ProductResponse(BaseModel):
    """Serializable product response."""

    id: str | None = None
    barcode: str | None = None
    name: str
    brand: str | None = None
    image_url: str | None = None
    label_image_url: str | None = None
    verification_tier: int = 3
    scan_count: int = 0
    vote_count: int = 0
    overall_status: str = "uncertain"
    confidence_level: str = "low"
