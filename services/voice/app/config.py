"""Settings, read once from the environment."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Absolute path, not ".env". A relative env_file resolves against the
    # working directory, and the service is launched from services/voice, so
    # a repo-root .env was silently ignored and every setting fell back to
    # its default. The failure is invisible: the app starts fine and simply
    # behaves as if nothing were configured.
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parents[3] / ".env"),
        extra="ignore",
    )

    database_url: str = "postgresql://operator:operator@localhost:5432/operator"
    redis_url: str = "redis://localhost:6379/0"
    app_env: str = "dev"
    public_base_url: str = ""

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_validate_signature: bool = True

    realtime_provider: str = "gemini"
    gemini_api_key: str = ""
    # Gemini 2.0 Flash models were retired in March 2026. Model ids churn on
    # the developer tier, so this is configurable and failures are explicit.
    gemini_live_model: str = "gemini-3.1-flash-live-preview"
    # Minimal thinking is the default for the lowest time-to-first-audio,
    # which is the number this whole product is judged on.
    gemini_thinking_level: str = "minimal"
    # Silence before the model treats a turn as finished. Added to every turn.
    gemini_end_of_speech_ms: int = 500
    # A cheap text model is plenty for structuring a pasted menu.
    gemini_import_model: str = "gemini-2.5-flash"
    gemini_voice: str = "Aoede"

    tool_timeout_ms: int = 1200
    max_clarify_attempts: int = 2
    twilio_from_number: str = ""
    # Comma-separated E.164 numbers. On a Twilio trial only verified numbers
    # receive messages, so this stops a demo failing on a Twilio error.
    demo_sms_allowlist: str = ""
    max_call_seconds: int = 600
    demo_mode: bool = True


def resolve_base_url(configured: str, env: dict[str, str]) -> str:
    """Normalise the public hostname, falling back to the platform's own.

    Split out from get_settings so it can be tested without depending on
    whether the developer running the suite happens to have a .env.
    """
    url = configured
    if not url:
        for var in ("RAILWAY_PUBLIC_DOMAIN", "FLY_APP_NAME"):
            value = (env.get(var) or "").strip()
            if value:
                url = value if "." in value else f"{value}.fly.dev"
                break
    return url.replace("https://", "").replace("http://", "").strip("/")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.public_base_url = resolve_base_url(
        settings.public_base_url, dict(os.environ)
    )
    return settings


def log_config_source() -> str:
    """Which .env was read, and whether it existed.

    An env file that silently is not found looks identical to one with every
    value left at its default, which is how a real call ends up answered by
    the mock provider.
    """
    path = Path(Settings.model_config["env_file"])
    return f"{path} ({'found' if path.is_file() else 'MISSING'})"
