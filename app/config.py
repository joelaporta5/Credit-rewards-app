"""Application configuration, read from environment variables (or a .env file).

Every secret lives in the environment, never in the code or the database.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Core ---
    app_env: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://postgres@localhost:5432/rewards_dev"
    # Public HTTPS address of this app, e.g. https://rewards-autopilot.onrender.com
    # Used to build the Plaid webhook URL and the OAuth redirect URL.
    public_base_url: str = "http://localhost:8000"
    timezone: str = "America/New_York"

    # --- Authentication ---
    # Long random key for the dashboard and admin API (header X-API-Key, or log in once in the browser).
    admin_api_key: str = Field(default="", repr=False)
    # Separate long random key used only by the iPhone Shortcut. It can only submit wallet events.
    wallet_api_key: str = Field(default="", repr=False)
    # Secret used to sign the browser session cookie.
    session_secret: str = Field(default="", repr=False)

    # --- Encryption of Plaid access tokens at rest ---
    # Either a Fernet key or any long random string (a Fernet key is derived from it).
    # Comma-separate several values to rotate keys: the first encrypts, all decrypt.
    token_encryption_key: str = Field(default="", repr=False)

    # --- Plaid ---
    plaid_env: Literal["sandbox", "production"] = "sandbox"
    plaid_client_id: str = Field(default="", repr=False)
    plaid_secret: str = Field(default="", repr=False)
    plaid_redirect_uri: str = ""  # optional; must be registered in the Plaid Dashboard if set
    plaid_days_requested: int = 90
    plaid_client_name: str = "Rewards Autopilot"

    # --- Background jobs ---
    scheduler_enabled: bool = True
    fallback_sync_hours: float = 6.0
    missing_event_check_minutes: float = 60.0
    # Don't call /accounts/balance/get more often than this per connection unless forced.
    balance_min_interval_minutes: float = 5.0

    @field_validator("public_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def webhook_url(self) -> str:
        return f"{self.public_base_url}/webhooks/plaid"

    @property
    def is_https(self) -> bool:
        return self.public_base_url.startswith("https://")

    def problems(self) -> list[str]:
        """Configuration problems that must be fixed before the app is safe to expose."""
        out = []
        for name in ("admin_api_key", "wallet_api_key", "session_secret", "token_encryption_key"):
            if len(getattr(self, name)) < 32:
                out.append(f"{name.upper()} must be set to a random value of at least 32 characters")
        if self.admin_api_key and self.admin_api_key == self.wallet_api_key:
            out.append("ADMIN_API_KEY and WALLET_API_KEY must be different")
        if self.app_env == "production" and not self.is_https:
            out.append("PUBLIC_BASE_URL must start with https:// in production")
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
