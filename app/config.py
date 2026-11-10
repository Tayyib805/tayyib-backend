"""Application configuration loaded from environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the Tayyib backend."""

    SUPABASE_URL: str
    SUPABASE_ANON_KEY: str
    SUPABASE_SERVICE_KEY: str
    ANTHROPIC_API_KEY: str
    # Change on the server (no code change needed) if a model is retired.
    ANTHROPIC_MODEL: str = "claude-sonnet-5-5"
    RESEND_API_KEY: str = ""
    OPEN_FOOD_FACTS_USER_AGENT: str = "tayyib/1.0"
    ENVIRONMENT: str = "development"
    CORS_ORIGINS: str = "*"
    # Phone notifications (web push). Leave empty to turn them off.
    VAPID_PUBLIC_KEY: str = ""
    VAPID_PRIVATE_KEY: str = ""
    VAPID_SUBJECT: str = "mailto:verify@tayyib.app"
    # Monthly open-data place files (built by .github/workflows/places-data.yml).
    PLACES_DATA_URL: str = "https://raw.githubusercontent.com/Tayyib805/tayyib-backend/places-data"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""

    return Settings()
