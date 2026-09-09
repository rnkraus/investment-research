"""Validated, secret-safe runtime settings for the M0.0 API."""

from functools import lru_cache
from ipaddress import ip_address
from os import environ
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from investment_research.modules.processing.contracts import (
    MAX_CONFIGURABLE_EXTRACTED_CHARACTERS,
    MAX_CONFIGURABLE_PDF_PAGES,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ROOT_ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_OBJECT_STORAGE_ROOT = PROJECT_ROOT / "output" / "object-storage"
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_CONFIGURABLE_UPLOAD_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_PDF_PAGES = 250
DEFAULT_MAX_EXTRACTED_CHARACTERS = 2_000_000
POSTGRES_SETTING_NAMES = (
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_PORT",
)


def _is_loopback_host(value: str) -> bool:
    """Return whether a host name or IP address resolves only to this machine."""

    if value == "localhost":
        return True

    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


class Settings(BaseModel):
    """Application settings loaded from process variables and local development values."""

    model_config = ConfigDict(frozen=True)

    app_name: str = "Investment Research Archive"
    environment: Literal["development", "test"] = "development"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1024, le=65535)
    database_url: SecretStr | None = None
    object_storage_root: Path = DEFAULT_OBJECT_STORAGE_ROOT
    max_upload_bytes: int = Field(
        default=DEFAULT_MAX_UPLOAD_BYTES,
        ge=1024,
        le=MAX_CONFIGURABLE_UPLOAD_BYTES,
    )
    max_pdf_pages: int = Field(
        default=DEFAULT_MAX_PDF_PAGES,
        ge=1,
        le=MAX_CONFIGURABLE_PDF_PAGES,
    )
    max_extracted_characters: int = Field(
        default=DEFAULT_MAX_EXTRACTED_CHARACTERS,
        ge=1,
        le=MAX_CONFIGURABLE_EXTRACTED_CHARACTERS,
    )

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
        if parsed_url.query:
            raise ValueError("database_url must not include query parameters during M0.0")
        if parsed_url.fragment:
            raise ValueError("database_url must not include a fragment")
        return value

    def require_database_url(self) -> str:
        """Return the database URL only for components that require PostgreSQL."""

        if self.database_url is None:
            raise RuntimeError("PostgreSQL configuration is required for database access")
        return self.database_url.get_secret_value()

    @field_validator("object_storage_root")
    @classmethod
    def require_private_object_storage_root(cls, value: Path) -> Path:
        """Resolve local storage predictably and keep it outside the web root."""

        resolved_root = (
            value.expanduser() if value.is_absolute() else PROJECT_ROOT / value.expanduser()
        ).resolve(strict=False)
        if resolved_root.is_relative_to(FRONTEND_ROOT):
            raise ValueError("object_storage_root must be outside the frontend web root")
        return resolved_root


def _configuration_value(
    name: str,
    file_values: dict[str, str | None],
    default: str | None = None,
) -> str | None:
    """Read a process value first, then its local-file fallback."""

    return environ.get(name, file_values.get(name, default))


def _local_database_url(file_values: dict[str, str | None]) -> str | None:
    """Build the local backend URL from the same PostgreSQL values used by Compose."""

    explicit_url = _configuration_value("INVESTMENT_RESEARCH_DATABASE_URL", file_values)
    if explicit_url is not None:
        return explicit_url

    postgres_values = {
        name: _configuration_value(name, file_values) for name in POSTGRES_SETTING_NAMES
    }
    if all(value is None for value in postgres_values.values()):
        return None

    missing_names = [name for name, value in postgres_values.items() if not value]
    if missing_names:
        raise ValueError(
            "local PostgreSQL configuration is incomplete; missing: " + ", ".join(missing_names)
        )

    user = postgres_values["POSTGRES_USER"]
    password = postgres_values["POSTGRES_PASSWORD"]
    database = postgres_values["POSTGRES_DB"]
    raw_port = postgres_values["POSTGRES_PORT"]
    assert user is not None
    assert password is not None
    assert database is not None
    assert raw_port is not None

    try:
        port = int(raw_port)
    except ValueError as error:
        raise ValueError("POSTGRES_PORT must be an integer") from error
    if not 1 <= port <= 65535:
        raise ValueError("POSTGRES_PORT must be between 1 and 65535")

    encoded_user = quote(user, safe="")
    encoded_password = quote(password, safe="")
    encoded_database = quote(database, safe="")
    return (
        f"postgresql+psycopg://{encoded_user}:{encoded_password}"
        f"@127.0.0.1:{port}/{encoded_database}"
    )


def load_settings(*, env_file: Path | None = ROOT_ENV_FILE) -> Settings:
    """Validate process variables with an optional local development file fallback."""

    file_values = (
        dict(dotenv_values(env_file)) if env_file is not None and env_file.is_file() else {}
    )

    values = {
        "app_name": _configuration_value(
            "INVESTMENT_RESEARCH_APP_NAME",
            file_values,
            "Investment Research Archive",
        ),
        "environment": _configuration_value(
            "INVESTMENT_RESEARCH_ENVIRONMENT",
            file_values,
            "development",
        ),
        "api_host": _configuration_value(
            "INVESTMENT_RESEARCH_API_HOST",
            file_values,
            "127.0.0.1",
        ),
        "api_port": _configuration_value("INVESTMENT_RESEARCH_API_PORT", file_values, "8000"),
        "database_url": _local_database_url(file_values),
        "object_storage_root": _configuration_value(
            "INVESTMENT_RESEARCH_OBJECT_STORAGE_ROOT",
            file_values,
            str(DEFAULT_OBJECT_STORAGE_ROOT),
        ),
        "max_upload_bytes": _configuration_value(
            "INVESTMENT_RESEARCH_MAX_UPLOAD_BYTES",
            file_values,
            str(DEFAULT_MAX_UPLOAD_BYTES),
        ),
        "max_pdf_pages": _configuration_value(
            "INVESTMENT_RESEARCH_MAX_PDF_PAGES",
            file_values,
            str(DEFAULT_MAX_PDF_PAGES),
        ),
        "max_extracted_characters": _configuration_value(
            "INVESTMENT_RESEARCH_MAX_EXTRACTED_CHARACTERS",
            file_values,
            str(DEFAULT_MAX_EXTRACTED_CHARACTERS),
        ),
    }
    return Settings.model_validate(values)


@lru_cache
def get_settings() -> Settings:
    """Return one immutable settings instance per process."""

    return load_settings()
