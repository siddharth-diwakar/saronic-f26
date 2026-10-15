"""Runtime configuration; .env is for local development, environment wins."""

from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    database_url: SecretStr
    app_env: Literal["development", "test", "production"] = "production"
    log_level: Literal["debug", "info", "warning", "error", "critical"] = "info"

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
            if url.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}:
                raise ValueError
            if not url.host or not url.database or not url.username:
                raise ValueError
        except (ArgumentError, ValueError):
            raise ValueError(
                "DATABASE_URL must be a PostgreSQL URL with user, host, and database"
            ) from None
        return value
