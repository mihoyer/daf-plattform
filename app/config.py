"""Zentrale Konfiguration der Plattform via .env-Datei."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # OpenAI
    openai_api_key: str = ""

    # Datenbank
    database_url: str = "postgresql+asyncpg://daf_user:daf_password@localhost:5432/daf_plattform"

    # Sicherheit
    secret_key: str = "change-me-in-production"
    admin_password: str = "admin"
    admin_username: str = "admin"

    # Stripe
    stripe_secret_key: str = "sk_test_placeholder"
    stripe_publishable_key: str = "pk_test_placeholder"
    stripe_webhook_secret: str = "whsec_placeholder"

    # App
    base_url: str = "http://localhost:8000"
    debug: bool = False
    max_audio_mb: int = 25
    max_image_mb: int = 10
    delete_audio_after_analysis: bool = True
    delete_image_after_analysis: bool = True
    session_expiry_days: int = 7

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
