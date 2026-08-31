from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", enable_decoding=False)

    database_url: str = "sqlite:///./tratte.db"
    gmail_label: str = "чеки"
    gmail_senders: list[str] = Field(
        default_factory=lambda: [
            "ofdreceipt@beeline.ru",
            "echeck@1-ofd.ru",
            "info@ofd-magnit.ru",
        ]
    )
    gmail_client_secret_file: Path = Path("client_secret.json")
    token_encryption_key: str = ""
    telegram_bot_token: str = ""
    telegram_allowed_user_ids: set[int] = Field(default_factory=set)
    sync_interval_minutes: int = 15
    default_timezone: str = "Europe/Moscow"
    log_level: str = "INFO"

    @field_validator("gmail_senders", mode="before")
    @classmethod
    def split_senders(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def split_user_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return {int(part.strip()) for part in value.split(",") if part.strip()}
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
