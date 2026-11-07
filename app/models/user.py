"""User profile request models."""

from pydantic import BaseModel


class UserProfileUpdate(BaseModel):
    """Payload for updating profile preferences.

    The app sends `madhab`; older clients send `madhab_preference`.
    """

    madhab_preference: str | None = None
    madhab: str | None = None
    madhab_set: bool | None = None
    active_theme: str | None = None
    active_companion: str | None = None
