"""User profile request models."""

from pydantic import BaseModel


class UserProfileUpdate(BaseModel):
    """Payload for updating profile preferences."""

    madhab_preference: str | None = None
    active_theme: str | None = None
    active_companion: str | None = None
