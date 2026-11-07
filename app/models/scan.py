"""Scan request and response models."""

from pydantic import BaseModel, Field


class ScanCreate(BaseModel):
    """Payload for recording a scan in the user's history.

    `product_id` is empty for barcodes we have not seen yet, so those scans
    still show up in Recent Scans.
    """

    product_id: str | None = None
    barcode: str | None = Field(default=None, max_length=32)
    scan_method: str | None = Field(default="barcode", max_length=16)
    country_code: str | None = Field(default=None, max_length=2)
    # The app sends the verdict it showed; stored as result_shown.
    verdict: str | None = Field(default=None, max_length=16)
    result_shown: str | None = Field(default=None, max_length=16)
    tier_at_time_of_scan: int | None = None
