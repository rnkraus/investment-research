"""Validated, non-secret runtime settings for the M0.0 API."""

from functools import lru_cache
from ipaddress import ip_address
from os import environ
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


def _is_loopback_host(value: str) -> bool:
    """Return whether a host name or IP address resolves only to this machine."""

    if value == "localhost":
        return True

    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


class Settings(BaseModel):
    """Application settings loaded from process environment variables."""

    model_config = ConfigDict(frozen=True)

    app_name: str = "Investment Research Archive"
    environment: Literal["development", "test"] = "development"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1024, le=65535)
    database_url: SecretStr | None = None

    @field_validator("api_host")
    @classmethod
    def require_loopback_host(cls, value: str) -> str:
        """Keep the M0.0 API bound to a loopback interface."""

        if not _is_loopback_host(value):
            raise ValueError("api_host must be localhost or a loopback IP address")
        return value

    @field_validator("database_url")
    @classmethod
    def require_local_postgresql_url(cls, value: SecretStr | None) -> SecretStr | None:
        """Accept only credentialed local Psycopg URLs during M0.0."""

        if value is None:
            return None

        raw_url = value.get_secret_value()
        try:
            parsed_url = urlsplit(raw_url)
            port = parsed_url.port
        except ValueError as error:
            raise ValueError("database_url must be a valid PostgreSQL URL") from error

        if parsed_url.scheme != "postgresql+psycopg":
            raise ValueError("database_url must use the postgresql+psycopg scheme")
        if parsed_url.hostname is None or not _is_loopback_host(parsed_url.hostname):
            raise ValueError("database_url must target localhost or a loopback IP address")
        if not parsed_url.username or not parsed_url.password:
            raise ValueError("database_url must include a username and password")
        if port is not None and port < 1:
            raise ValueError("database_url port must be between 1 and 65535")
        if parsed_url.path in {"", "/"}:
            raise ValueError("database_url must include a database name")
        if parsed_url.fragment:
            raise ValueError("database_url must not include a fragment")
        return value

    def require_database_url(self) -> str:
        """Return the database URL only for components that require PostgreSQL."""

        if self.database_url is None:
            raise RuntimeError("INVESTMENT_RESEARCH_DATABASE_URL is required for database access")
        return self.database_url.get_secret_value()


def load_settings() -> Settings:
    """Validate supported environment variables without reading an environment file."""

    values = {
        "app_name": environ.get("INVESTMENT_RESEARCH_APP_NAME", "Investment Research Archive"),
        "environment": environ.get("INVESTMENT_RESEARCH_ENVIRONMENT", "development"),
        "api_host": environ.get("INVESTMENT_RESEARCH_API_HOST", "127.0.0.1"),
        "api_port": environ.get("INVESTMENT_RESEARCH_API_PORT", "8000"),
        "database_url": environ.get("INVESTMENT_RESEARCH_DATABASE_URL"),
    }
    return Settings.model_validate(values)


@lru_cache
def get_settings() -> Settings:
    """Return one immutable settings instance per process."""

    return load_settings()
