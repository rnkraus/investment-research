"""Validated, non-secret runtime settings for the M0.0 API."""

from functools import lru_cache
from ipaddress import ip_address
from os import environ
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Settings(BaseModel):
    """Application settings loaded from process environment variables."""

    model_config = ConfigDict(frozen=True)

    app_name: str = "Investment Research Archive"
    environment: Literal["development", "test"] = "development"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1024, le=65535)

    @field_validator("api_host")
    @classmethod
    def require_loopback_host(cls, value: str) -> str:
        """Keep the M0.0 API bound to a loopback interface."""

        if value == "localhost":
            return value

        try:
            address = ip_address(value)
        except ValueError as error:
            raise ValueError("api_host must be localhost or a loopback IP address") from error

        if not address.is_loopback:
            raise ValueError("api_host must be localhost or a loopback IP address")
        return value


def load_settings() -> Settings:
    """Validate supported environment variables without reading an environment file."""

    values = {
        "app_name": environ.get("INVESTMENT_RESEARCH_APP_NAME", "Investment Research Archive"),
        "environment": environ.get("INVESTMENT_RESEARCH_ENVIRONMENT", "development"),
        "api_host": environ.get("INVESTMENT_RESEARCH_API_HOST", "127.0.0.1"),
        "api_port": environ.get("INVESTMENT_RESEARCH_API_PORT", "8000"),
    }
    return Settings.model_validate(values)


@lru_cache
def get_settings() -> Settings:
    """Return one immutable settings instance per process."""

    return load_settings()
