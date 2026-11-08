"""User profile request models."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Madhab = Literal["general", "hanafi", "shafii", "maliki", "hanbali", "strict"]


class UserProfileUpdate(BaseModel):
    """Payload for updating profile preferences.

    The app sends `madhab`; older clients send `madhab_preference`.
    """

    madhab_preference: Madhab | None = None
    madhab: Madhab | None = None
    madhab_set: bool | None = None
    active_theme: str | None = Field(default=None, max_length=40, pattern=r"^[a-z0-9_-]+$")
    active_companion: str | None = Field(default=None, max_length=20)
    leaderboard_visible: bool | None = None
    # Public name for the leaderboard and comments. An empty string clears it.
    display_name: str | None = Field(default=None, max_length=60)

    @field_validator("madhab", "madhab_preference", mode="before")
    @classmethod
    def _old_spelling(cls, value):
        return "shafii" if value == "shafi" else value
