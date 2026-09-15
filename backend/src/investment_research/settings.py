"""Validated, process-specific runtime settings."""

from functools import lru_cache
from ipaddress import ip_address
from os import environ
from pathlib import Path
from re import fullmatch
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
type ObjectStorageProvider = Literal["local", "s3"]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ROOT_ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_OBJECT_STORAGE_ROOT = PROJECT_ROOT / "output" / "object-storage"
DEFAULT_S3_REGION_NAME = "us-east-1"
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
DOCKER_SECRETS_ROOT = Path("/run/secrets")
POSTGRES_SETTING_NAMES = (
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_PORT",
)
REDIS_SETTING_NAMES = (
    "REDIS_PASSWORD",
    "REDIS_PORT",
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
    "REDIS_",
    "MINIO_",
    "AWS_",
    "INVESTMENT_RESEARCH_S3_",
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

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

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


class QueueSettings(DatabaseSettings):
    """Shared configuration for API and worker access to the transient broker."""

    redis_url: SecretStr | None = None

    @model_validator(mode="after")
    def require_supported_redis_url(self) -> QueueSettings:
        """Restrict M0 broker access to the authenticated local runtime topology."""

        if self.redis_url is None:
            return self

        raw_url = self.redis_url.get_secret_value()
        try:
            parsed_url = urlsplit(raw_url)
            port = parsed_url.port
        except ValueError as error:
            raise ValueError("redis_url must be a valid Redis URL") from error

        if parsed_url.scheme != "redis":
            raise ValueError("redis_url must use the redis scheme")
        if self.runtime_location == "container":
            valid_host = parsed_url.hostname == "redis"
            target = "the Compose redis service"
        else:
            valid_host = parsed_url.hostname is not None and _is_loopback_host(parsed_url.hostname)
            target = "localhost or a loopback IP address"
        if not valid_host:
            raise ValueError(f"redis_url must target {target}")
        if parsed_url.username not in {None, ""}:
            raise ValueError("redis_url must use the password-only M0 authentication format")
        if not parsed_url.password:
            raise ValueError("redis_url must include a password")
        if port is not None and port < 1:
            raise ValueError("redis_url port must be between 1 and 65535")
        if parsed_url.path != "/0":
            raise ValueError("redis_url must select the M0 broker database /0")
        if parsed_url.query:
            raise ValueError("redis_url must not include query parameters")
        if parsed_url.fragment:
            raise ValueError("redis_url must not include a fragment")
        return self

    def require_redis_url(self) -> str:
        """Return the broker URL only for components that require queue access."""

        if self.redis_url is None:
            raise RuntimeError("Redis configuration is required for queue access")
        return self.redis_url.get_secret_value()


class S3Settings(BaseModel):
    """Validated S3 data-plane configuration shared by API and worker processes."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    endpoint_url: str
    region_name: str = DEFAULT_S3_REGION_NAME
    bucket: str
    access_key_id: SecretStr
    secret_access_key: SecretStr

    @field_validator("region_name")
    @classmethod
    def require_normalized_region_name(cls, value: str) -> str:
        """Accept one non-empty normalized region identifier."""

        if not fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value):
            raise ValueError("region_name must be a normalized lowercase identifier")
        return value

    @field_validator("bucket")
    @classmethod
    def require_supported_bucket_name(cls, value: str) -> str:
        """Restrict buckets to portable DNS-compatible S3 names."""

        valid = (
            3 <= len(value) <= 63
            and fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]", value) is not None
            and ".." not in value
            and ".-" not in value
            and "-." not in value
        )
        if not valid:
            raise ValueError("bucket must be a DNS-compatible S3 bucket name")
        try:
            ip_address(value)
        except ValueError:
            return value
        raise ValueError("bucket must not be formatted as an IP address")

    @field_validator("access_key_id", "secret_access_key")
    @classmethod
    def require_non_empty_secret(cls, value: SecretStr) -> SecretStr:
        """Reject blank credentials without exposing their values."""

        secret = value.get_secret_value()
        if not secret or secret != secret.strip():
            raise ValueError("S3 credentials must be non-empty normalized values")
        return value


class ObjectStorageSettings(QueueSettings):
    """Configuration shared by processes that access canonical originals."""

    object_storage_provider: ObjectStorageProvider = "local"
    object_storage_root: Path = DEFAULT_OBJECT_STORAGE_ROOT
    s3: S3Settings | None = None

    @field_validator("object_storage_root")
    @classmethod
    def require_private_object_storage_root(cls, value: Path) -> Path:
        """Resolve test-only local storage and keep it outside the web root."""

        return _resolve_private_object_storage_root(value)

    @model_validator(mode="after")
    def require_matching_object_storage_configuration(self) -> ObjectStorageSettings:
        """Require one complete provider configuration without unused S3 secrets."""

        if self.object_storage_provider == "local":
            if self.s3 is not None:
                raise ValueError("s3 configuration is not allowed for the local provider")
            return self

        if self.s3 is None:
            raise ValueError("s3 configuration is required for the s3 provider")

        try:
            endpoint = urlsplit(self.s3.endpoint_url)
            port = endpoint.port
        except ValueError as error:
            raise ValueError("s3 endpoint_url must be a valid HTTP URL") from error
        if endpoint.scheme not in {"http", "https"}:
            raise ValueError("s3 endpoint_url must use HTTP or HTTPS")
        if endpoint.username or endpoint.password:
            raise ValueError("s3 endpoint_url must not contain credentials")
        if endpoint.path not in {"", "/"} or endpoint.query or endpoint.fragment:
            raise ValueError("s3 endpoint_url must not contain a path, query, or fragment")
        if port is not None and port < 1:
            raise ValueError("s3 endpoint_url port must be between 1 and 65535")

        if self.runtime_location == "container":
            valid_host = endpoint.hostname == "seaweedfs-s3"
            target = "the Compose seaweedfs-s3 service"
        else:
            valid_host = endpoint.hostname is not None and _is_loopback_host(endpoint.hostname)
            target = "localhost or a loopback IP address"
        if not valid_host:
            raise ValueError(f"s3 endpoint_url must target {target}")
        return self

    def require_s3_settings(self) -> S3Settings:
        """Return complete S3 values only when the S3 provider is selected."""

        if self.object_storage_provider != "s3" or self.s3 is None:
            raise RuntimeError("S3 configuration is required for S3 object storage")
        return self.s3


class ApiSettings(ObjectStorageSettings):
    """Configuration available to the FastAPI process."""

    app_name: str = "Investment Research Archive"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1024, le=65535)
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


class WorkerSettings(ObjectStorageSettings):
    """Configuration available to the M0.0 runner and later Celery worker."""

    parser_endpoint_url: str | None = None
    max_parser_request_bytes: int = Field(
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

    @model_validator(mode="after")
    def require_supported_parser_endpoint(self) -> WorkerSettings:
        """Restrict parser traffic to the isolated local runtime endpoint."""

        if self.parser_endpoint_url is None:
            return self
        try:
            endpoint = urlsplit(self.parser_endpoint_url)
            port = endpoint.port
        except ValueError as error:
            raise ValueError("parser_endpoint_url must be a valid HTTP URL") from error
        if endpoint.scheme != "http":
            raise ValueError("parser_endpoint_url must use HTTP")
        if endpoint.username or endpoint.password:
            raise ValueError("parser_endpoint_url must not contain credentials")
        if endpoint.path not in {"", "/"} or endpoint.query or endpoint.fragment:
            raise ValueError("parser_endpoint_url must not contain a path, query, or fragment")
        if port is not None and port < 1:
            raise ValueError("parser_endpoint_url port must be between 1 and 65535")

        if self.runtime_location == "container":
            valid_host = endpoint.hostname == "parser"
            target = "the Compose parser service"
        else:
            valid_host = endpoint.hostname is not None and _is_loopback_host(endpoint.hostname)
            target = "localhost or a loopback IP address"
        if not valid_host:
            raise ValueError(f"parser_endpoint_url must target {target}")
        return self

    def require_parser_endpoint_url(self) -> str:
        """Return the internal endpoint only for workers using isolated parsing."""

        if self.parser_endpoint_url is None:
            raise RuntimeError("parser endpoint configuration is required")
        return self.parser_endpoint_url


class ParserSettings(RuntimeSettings):
    """Non-secret configuration available to the isolated parser process."""

    parser_host: str = "127.0.0.1"
    parser_port: int = Field(default=DEFAULT_PARSER_PORT, ge=1024, le=65535)
    max_request_bytes: int = Field(
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


def _secret_configuration_value(
    name: str,
    file_values: dict[str, str | None],
) -> str | None:
    """Read a direct local value or one explicitly mounted Docker secret."""

    direct_value = _configuration_value(name, file_values)
    file_variable = f"{name}_FILE"
    secret_path_value = environ.get(file_variable)
    if secret_path_value is None:
        return direct_value
    if direct_value is not None:
        raise ValueError(f"{name} and {file_variable} cannot both be configured")

    secret_path = Path(secret_path_value)
    if secret_path.parent != DOCKER_SECRETS_ROOT or secret_path.name in {"", ".", ".."}:
        raise ValueError(f"{file_variable} must name one file directly under /run/secrets")
    if secret_path.is_symlink():
        raise ValueError(f"{file_variable} must not reference a symbolic link")
    try:
        value = secret_path.read_text(encoding="utf-8").rstrip("\n")
    except OSError as error:
        raise ValueError(f"{file_variable} is unavailable") from error
    if not value:
        raise ValueError(f"{file_variable} is empty")
    return value


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

    explicit_url = _secret_configuration_value("INVESTMENT_RESEARCH_DATABASE_URL", file_values)
    if explicit_url is not None:
        return explicit_url

    postgres_values = {
        name: (
            _secret_configuration_value(name, file_values)
            if name == "POSTGRES_PASSWORD"
            else _configuration_value(name, file_values)
        )
        for name in POSTGRES_SETTING_NAMES
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
    database_host = "postgres" if runtime_location == "container" else "127.0.0.1"
    return (
        f"postgresql+psycopg://{encoded_user}:{encoded_password}"
        f"@{database_host}:{port}/{encoded_database}"
    )


def _database_runtime_values(file_values: dict[str, str | None]) -> dict[str, object]:
    """Load common values plus the canonical PostgreSQL connection URL."""

    values: dict[str, object] = {**_runtime_values(file_values)}
    values["database_url"] = _database_url(file_values, values.get("runtime_location"))
    return values


def _redis_url(file_values: dict[str, str | None], runtime_location: object) -> str | None:
    """Load an explicit URL or construct the host-development Redis URL."""

    explicit_url = _secret_configuration_value("INVESTMENT_RESEARCH_REDIS_URL", file_values)
    if explicit_url is not None:
        return explicit_url

    redis_values = {
        name: (
            _secret_configuration_value(name, file_values)
            if name == "REDIS_PASSWORD"
            else _configuration_value(name, file_values)
        )
        for name in REDIS_SETTING_NAMES
    }
    if all(value is None for value in redis_values.values()):
        return None

    missing_names = [name for name, value in redis_values.items() if not value]
    if missing_names:
        raise ValueError(
            "local Redis configuration is incomplete; missing: " + ", ".join(missing_names)
        )

    password = redis_values["REDIS_PASSWORD"]
    raw_port = redis_values["REDIS_PORT"]
    assert password is not None
    assert raw_port is not None

    try:
        port = int(raw_port)
    except ValueError as error:
        raise ValueError("REDIS_PORT must be an integer") from error
    if not 1 <= port <= 65535:
        raise ValueError("REDIS_PORT must be between 1 and 65535")

    redis_host = "redis" if runtime_location == "container" else "127.0.0.1"
    return f"redis://:{quote(password, safe='')}@{redis_host}:{port}/0"


def _queue_runtime_values(
    file_values: dict[str, str | None],
    runtime_location: object,
) -> dict[str, object]:
    """Load the transient Redis broker URL for API and worker processes."""

    return {
        "redis_url": _redis_url(file_values, runtime_location),
    }


def _object_storage_runtime_values(file_values: dict[str, str | None]) -> dict[str, object]:
    """Load provider selection and one optional complete S3 configuration."""

    values: dict[str, object] = {
        "object_storage_provider": _configuration_value(
            "INVESTMENT_RESEARCH_OBJECT_STORAGE_PROVIDER",
            file_values,
            "local",
        ),
        "object_storage_root": _configuration_value(
            "INVESTMENT_RESEARCH_OBJECT_STORAGE_ROOT",
            file_values,
            str(DEFAULT_OBJECT_STORAGE_ROOT),
        ),
    }
    s3_values = {
        "endpoint_url": _configuration_value(
            "INVESTMENT_RESEARCH_S3_ENDPOINT_URL",
            file_values,
        ),
        "region_name": _configuration_value(
            "INVESTMENT_RESEARCH_S3_REGION_NAME",
            file_values,
        ),
        "bucket": _configuration_value(
            "INVESTMENT_RESEARCH_S3_BUCKET",
            file_values,
        ),
        "access_key_id": _secret_configuration_value(
            "INVESTMENT_RESEARCH_S3_ACCESS_KEY_ID",
            file_values,
        ),
        "secret_access_key": _secret_configuration_value(
            "INVESTMENT_RESEARCH_S3_SECRET_ACCESS_KEY",
            file_values,
        ),
    }
    configured_s3_values = {name: value for name, value in s3_values.items() if value is not None}
    if configured_s3_values:
        values["s3"] = configured_s3_values
    return values


def load_database_settings(*, env_file: Path | None = ROOT_ENV_FILE) -> DatabaseSettings:
    """Load only values required for a direct PostgreSQL maintenance process."""

    return DatabaseSettings.model_validate(_database_runtime_values(_read_local_values(env_file)))


def load_api_settings(*, env_file: Path | None = ROOT_ENV_FILE) -> ApiSettings:
    """Load only values required by the FastAPI process."""

    file_values = _read_local_values(env_file)
    database_values = _database_runtime_values(file_values)
    values = {
        **database_values,
        **_queue_runtime_values(file_values, database_values.get("runtime_location")),
        **_object_storage_runtime_values(file_values),
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
    database_values = _database_runtime_values(file_values)
    values = {
        **database_values,
        **_queue_runtime_values(file_values, database_values.get("runtime_location")),
        **_object_storage_runtime_values(file_values),
        "max_pdf_pages": _configuration_value(
            "INVESTMENT_RESEARCH_MAX_PDF_PAGES",
            file_values,
            str(DEFAULT_MAX_PDF_PAGES),
        ),
        "parser_endpoint_url": _configuration_value(
            "INVESTMENT_RESEARCH_PARSER_ENDPOINT_URL",
            file_values,
        ),
        "max_parser_request_bytes": _configuration_value(
            "INVESTMENT_RESEARCH_MAX_UPLOAD_BYTES",
            file_values,
            str(DEFAULT_MAX_UPLOAD_BYTES),
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
        "max_request_bytes": _configuration_value(
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
