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
    # ── Launch switches (set on the server; no code change) ──────────────
    # Map tiles: a provider URL with {z}/{x}/{y} (and its key). Empty = OpenStreetMap's
    # own tiles, which are fine for testing but not meant for a busy app.
    MAP_TILE_URL: str = ""
    MAP_TILE_URL_DARK: str = ""
    MAP_TILE_ATTRIBUTION: str = ""
    # Which design the app shows everyone: "new" or "classic". Both ship in the same
    # app build, so changing this takes effect the next time each phone opens the app.
    # To go back to the original look, set APP_DESIGN=classic.
    APP_DESIGN: str = "new"
    # Show "Sign in with Apple" once the provider is set up in Supabase Auth.
    APPLE_SIGN_IN_ENABLED: bool = False
    # Subscriptions (RevenueCat). The webhook is refused until REVENUECAT_WEBHOOK_AUTH is set.
    SUBSCRIPTIONS_OPEN: bool = False
    REVENUECAT_WEBHOOK_AUTH: str = ""
    REVENUECAT_API_KEY: str = ""           # secret key: lets us ask RevenueCat for the latest state
    REVENUECAT_PUBLIC_WEB_KEY: str = ""    # public key for buying on the web
    REVENUECAT_ENTITLEMENT_STANDARD: str = "standard"
    REVENUECAT_ENTITLEMENT_PRO: str = "pro"
    REVENUECAT_ACCEPT_SANDBOX: bool = False

    # Monthly open-data place files (built by .github/workflows/places-data.yml).
    PLACES_DATA_URL: str = "https://raw.githubusercontent.com/Tayyib805/tayyib-backend/places-data"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


DESIGNS = ("new", "classic")


def design_version(value: str | None) -> str:
    """The design to serve. Anything we don't recognise (a typo, an empty value) falls back to the default."""

    wanted = (value or "").strip().lower()
    return wanted if wanted in DESIGNS else DESIGNS[0]


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""

    return Settings()
