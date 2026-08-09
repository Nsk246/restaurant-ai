"""Settings, read once from the environment."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://operator:operator@localhost:5432/operator"
    redis_url: str = "redis://localhost:6379/0"
    app_env: str = "dev"
    public_base_url: str = ""

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_validate_signature: bool = True

    realtime_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_live_model: str = "gemini-2.0-flash-live-001"
    gemini_voice: str = "Aoede"

    max_call_seconds: int = 600
    demo_mode: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
