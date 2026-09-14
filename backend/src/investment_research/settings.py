"""Validated, process-specific runtime settings."""

from functools import lru_cache
from ipaddress import ip_address
from os import environ
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from investment_research.modules.processing.contracts import (
    MAX_CONFIGURABLE_EXTRACTED_CHARACTERS,
    MAX_CONFIGURABLE_PDF_PAGES,
)

type RuntimeEnvironment = Literal["development", "test"]
type RuntimeLocation = Literal["host", "container"]
type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ROOT_ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_OBJECT_STORAGE_ROOT = PROJECT_ROOT / "output" / "object-storage"
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_CONFIGURABLE_UPLOAD_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_PDF_PAGES = 250
DEFAULT_MAX_EXTRACTED_CHARACTERS = 2_000_000
DEFAULT_PROCESSING_TIMEOUT_SECONDS = 30
MAX_CONFIGURABLE_PROCESSING_TIMEOUT_SECONDS = 300
DEFAULT_PROCESSING_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
MIN_PROCESSING_MEMORY_LIMIT_BYTES = 64 * 1024 * 1024
MAX_CONFIGURABLE_PROCESSING_MEMORY_LIMIT_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_PARSER_PORT = 8100
POSTGRES_SETTING_NAMES = (
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_PORT",
)
PARSER_FORBIDDEN_ENVIRONMENT_NAMES = frozenset(
    {
        "INVESTMENT_RESEARCH_DATABASE_URL",
        "INVESTMENT_RESEARCH_REDIS_URL",
        "INVESTMENT_RESEARCH_S3_ENDPOINT_URL",
        "INVESTMENT_RESEARCH_S3_ACCESS_KEY",
        "INVESTMENT_RESEARCH_S3_SECRET_KEY",
        "CELERY_BROKER_URL",
    }
)
PARSER_FORBIDDEN_ENVIRONMENT_PREFIXES = (
    "POSTGRES_",
    "MINIO_",
    "AWS_",
    "OPENAI_",
    "LANGSMITH_",
)


def _is_loopback_host(value: str) -> bool:
    """Return whether a host name or IP address resolves only to this machine."""

    if value == "localhost":
        return True

    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


def _is_container_bind_host(value: str) -> bool:
    """Return whether a service may bind to this address inside its container."""

    return _is_loopback_host(value) or value == "0.0.0.0"


def _resolve_private_object_storage_root(value: Path) -> Path:
    """Resolve a local object root and keep it outside the frontend web root."""

    resolved_root = (
        value.expanduser() if value.is_absolute() else PROJECT_ROOT / value.expanduser()
    ).resolve(strict=False)
    if resolved_root.is_relative_to(FRONTEND_ROOT):
        raise ValueError("object_storage_root must be outside the frontend web root")
    return resolved_root


class RuntimeSettings(BaseModel):
    """Values shared by all application-owned runtime processes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: RuntimeEnvironment = "development"
    runtime_location: RuntimeLocation = "host"
    log_level: LogLevel = "INFO"


class DatabaseSettings(RuntimeSettings):
    """Shared configuration for processes that own PostgreSQL access."""

    database_url: SecretStr | None = None

    @model_validator(mode="after")
    def require_supported_postgresql_url(self) -> DatabaseSettings:
        """Restrict PostgreSQL targets to the active local runtime topology."""

        if self.database_url is None:
            return self

        raw_url = self.database_url.get_secret_value()
        try:
            parsed_url = urlsplit(raw_url)
            port = parsed_url.port
        except ValueError as error:
            raise ValueError("database_url must be a valid PostgreSQL URL") from error

        if parsed_url.scheme != "postgresql+psycopg":
            raise ValueError("database_url must use the postgresql+psycopg scheme")
        if self.runtime_location == "container":
            valid_host = parsed_url.hostname == "postgres"
            target = "the Compose postgres service"
        else:
            valid_host = parsed_url.hostname is not None and _is_loopback_host(parsed_url.hostname)
            target = "localhost or a loopback IP address"
        if not valid_host:
            raise ValueError(f"database_url must target {target}")
        if not parsed_url.username or not parsed_url.password:
            raise ValueError("database_url must include a username and password")
        if port is not None and port < 1:
            raise ValueError("database_url port must be between 1 and 65535")
        if parsed_url.path in {"", "/"}:
            raise ValueError("database_url must include a database name")
        if parsed_url.query:
            raise ValueError("database_url must not include query parameters")
        if parsed_url.fragment:
            raise ValueError("database_url must not include a fragment")
        return self

    def require_database_url(self) -> str:
        """Return the database URL only for components that require PostgreSQL."""

        if self.database_url is None:
            raise RuntimeError("PostgreSQL configuration is required for database access")
        return self.database_url.get_secret_value()


class ApiSettings(DatabaseSettings):
    """Configuration available to the FastAPI process."""

    app_name: str = "Investment Research Archive"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1024, le=65535)
    object_storage_root: Path = DEFAULT_OBJECT_STORAGE_ROOT
    max_upload_bytes: int = Field(
        default=DEFAULT_MAX_UPLOAD_BYTES,
        ge=1024,
        le=MAX_CONFIGURABLE_UPLOAD_BYTES,
    )

    @model_validator(mode="after")
    def require_supported_api_host(self) -> ApiSettings:
        """Permit a wildcard bind only inside the container runtime."""

        if self.runtime_location == "container":
            valid = _is_container_bind_host(self.api_host)
        else:
            valid = _is_loopback_host(self.api_host)
        if not valid:
            raise ValueError("api_host is not allowed for the selected runtime location")
        return self

    @field_validator("object_storage_root")
    @classmethod
    def require_private_object_storage_root(cls, value: Path) -> Path:
        """Resolve local storage predictably and keep it outside the web root."""

        return _resolve_private_object_storage_root(value)


class WorkerSettings(DatabaseSettings):
    """Configuration available to the M0.0 runner and later Celery worker."""

    object_storage_root: Path = DEFAULT_OBJECT_STORAGE_ROOT
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
    processing_timeout_seconds: int = Field(
        default=DEFAULT_PROCESSING_TIMEOUT_SECONDS,
        ge=1,
        le=MAX_CONFIGURABLE_PROCESSING_TIMEOUT_SECONDS,
    )
    processing_memory_limit_bytes: int = Field(
        default=DEFAULT_PROCESSING_MEMORY_LIMIT_BYTES,
        ge=MIN_PROCESSING_MEMORY_LIMIT_BYTES,
        le=MAX_CONFIGURABLE_PROCESSING_MEMORY_LIMIT_BYTES,
    )

    @field_validator("object_storage_root")
    @classmethod
    def require_private_object_storage_root(cls, value: Path) -> Path:
        """Resolve local storage predictably and keep it outside the web root."""

        return _resolve_private_object_storage_root(value)


class ParserSettings(RuntimeSettings):
    """Non-secret configuration available to the isolated parser process."""

    parser_host: str = "127.0.0.1"
    parser_port: int = Field(default=DEFAULT_PARSER_PORT, ge=1024, le=65535)
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

    @model_validator(mode="after")
    def require_supported_parser_host(self) -> ParserSettings:
        """Permit the internal listener only on an appropriate local bind address."""

        if self.runtime_location == "container":
            valid = _is_container_bind_host(self.parser_host)
        else:
            valid = _is_loopback_host(self.parser_host)
        if not valid:
            raise ValueError("parser_host is not allowed for the selected runtime location")
        return self


def _configuration_value(
    name: str,
    file_values: dict[str, str | None],
    default: str | None = None,
) -> str | None:
    """Read a process value first, then its local-file fallback."""

    return environ.get(name, file_values.get(name, default))


def _read_local_values(env_file: Path | None) -> dict[str, str | None]:
    """Read an explicitly allowed local development file when it exists."""

    if env_file is None or not env_file.is_file():
        return {}
    return dict(dotenv_values(env_file))


def _runtime_values(file_values: dict[str, str | None]) -> dict[str, str | None]:
    """Load the small configuration set shared by all runtime processes."""

    return {
        "environment": _configuration_value(
            "INVESTMENT_RESEARCH_ENVIRONMENT",
            file_values,
            "development",
        ),
        "runtime_location": _configuration_value(
            "INVESTMENT_RESEARCH_RUNTIME_LOCATION",
            file_values,
            "host",
        ),
        "log_level": _configuration_value(
            "INVESTMENT_RESEARCH_LOG_LEVEL",
            file_values,
            "INFO",
        ),
    }


def _database_url(file_values: dict[str, str | None], runtime_location: object) -> str | None:
    """Load an explicit URL or construct the host-development PostgreSQL URL."""

    explicit_url = _configuration_value("INVESTMENT_RESEARCH_DATABASE_URL", file_values)
    if explicit_url is not None:
        return explicit_url

    postgres_values = {
        name: _configuration_value(name, file_values) for name in POSTGRES_SETTING_NAMES
    }
    if all(value is None for value in postgres_values.values()):
        return None
    if runtime_location == "container":
        raise ValueError("container application processes require INVESTMENT_RESEARCH_DATABASE_URL")

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


def _database_runtime_values(file_values: dict[str, str | None]) -> dict[str, object]:
    """Load common values plus the canonical PostgreSQL connection URL."""

    values: dict[str, object] = {**_runtime_values(file_values)}
    values["database_url"] = _database_url(file_values, values.get("runtime_location"))
    return values


def load_database_settings(*, env_file: Path | None = ROOT_ENV_FILE) -> DatabaseSettings:
    """Load only values required for a direct PostgreSQL maintenance process."""

    return DatabaseSettings.model_validate(_database_runtime_values(_read_local_values(env_file)))


def load_api_settings(*, env_file: Path | None = ROOT_ENV_FILE) -> ApiSettings:
    """Load only values required by the FastAPI process."""

    file_values = _read_local_values(env_file)
    values = {
        **_database_runtime_values(file_values),
        "app_name": _configuration_value(
            "INVESTMENT_RESEARCH_APP_NAME",
            file_values,
            "Investment Research Archive",
        ),
        "api_host": _configuration_value(
            "INVESTMENT_RESEARCH_API_HOST",
            file_values,
            "127.0.0.1",
        ),
        "api_port": _configuration_value("INVESTMENT_RESEARCH_API_PORT", file_values, "8000"),
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
    }
    return ApiSettings.model_validate(values)


def load_worker_settings(*, env_file: Path | None = ROOT_ENV_FILE) -> WorkerSettings:
    """Load only values required by the M0.0 runner and later Celery worker."""

    file_values = _read_local_values(env_file)
    values = {
        **_database_runtime_values(file_values),
        "object_storage_root": _configuration_value(
            "INVESTMENT_RESEARCH_OBJECT_STORAGE_ROOT",
            file_values,
            str(DEFAULT_OBJECT_STORAGE_ROOT),
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
        "processing_timeout_seconds": _configuration_value(
            "INVESTMENT_RESEARCH_PROCESSING_TIMEOUT_SECONDS",
            file_values,
            str(DEFAULT_PROCESSING_TIMEOUT_SECONDS),
        ),
        "processing_memory_limit_bytes": _configuration_value(
            "INVESTMENT_RESEARCH_PROCESSING_MEMORY_LIMIT_BYTES",
            file_values,
            str(DEFAULT_PROCESSING_MEMORY_LIMIT_BYTES),
        ),
    }
    return WorkerSettings.model_validate(values)


def _reject_parser_credentials() -> None:
    """Fail closed when known application credential variables reach the parser."""

    forbidden_names = sorted(
        name
        for name in environ
        if name in PARSER_FORBIDDEN_ENVIRONMENT_NAMES
        or name.startswith(PARSER_FORBIDDEN_ENVIRONMENT_PREFIXES)
    )
    if forbidden_names:
        raise ValueError(
            "parser environment contains forbidden application configuration: "
            + ", ".join(forbidden_names)
        )


def load_parser_settings() -> ParserSettings:
    """Load non-secret parser values from the process environment only."""

    _reject_parser_credentials()
    file_values: dict[str, str | None] = {}
    values = {
        **_runtime_values(file_values),
        "parser_host": _configuration_value(
            "INVESTMENT_RESEARCH_PARSER_HOST",
            file_values,
            "127.0.0.1",
        ),
        "parser_port": _configuration_value(
            "INVESTMENT_RESEARCH_PARSER_PORT",
            file_values,
            str(DEFAULT_PARSER_PORT),
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
    return ParserSettings.model_validate(values)


@lru_cache
def get_api_settings() -> ApiSettings:
    """Return one immutable API settings instance per process."""

    return load_api_settings()


@lru_cache
def get_worker_settings() -> WorkerSettings:
    """Return one immutable worker settings instance per process."""

    return load_worker_settings()


@lru_cache
def get_parser_settings() -> ParserSettings:
    """Return one immutable parser settings instance per process."""

    return load_parser_settings()
