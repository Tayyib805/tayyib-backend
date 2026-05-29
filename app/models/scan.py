"""Scan request and response models."""

from pydantic import BaseModel


class ScanCreate(BaseModel):
    """Payload for recording a scan."""

    product_id: str
    result_shown: str | None = None
    tier_at_time_of_scan: int | None = None
