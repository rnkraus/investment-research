"""Load process-specific runtime configuration and re-export validated settings models."""

from functools import lru_cache
from os import environ
from pathlib import Path
from urllib.parse import quote

from dotenv import dotenv_values

from investment_research.settings_models import (
    DEFAULT_DENSE_INDEX_CONFIG_VERSION,
    DEFAULT_EMBEDDING_CONFIG_VERSION,
    DEFAULT_EMBEDDING_DIMENSIONS,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_TIMEOUT_SECONDS,
    DEFAULT_MAX_EXTRACTED_CHARACTERS,
    DEFAULT_MAX_PDF_PAGES,
    DEFAULT_MAX_UPLOAD_BYTES,
    DEFAULT_OBJECT_STORAGE_ROOT,
    DEFAULT_PARSER_CHILD_TIMEOUT_SECONDS,
    DEFAULT_PARSER_MAX_CONCURRENT_CHILDREN,
    DEFAULT_PARSER_PORT,
    DEFAULT_PINECONE_OPERATION_TIMEOUT_SECONDS,
    DEFAULT_PROCESSING_TIMEOUT_SECONDS,
    DEFAULT_S3_REGION_NAME,
    DOCKER_SECRETS_ROOT,
    FRONTEND_ROOT,
    M1_MAX_PROVIDER_MONTHLY_BUDGET_MICRO_USD,
    MAX_CONFIGURABLE_PARSER_CHILD_TIMEOUT_SECONDS,
    MAX_CONFIGURABLE_PARSER_CONCURRENT_CHILDREN,
    MAX_CONFIGURABLE_PROCESSING_TIMEOUT_SECONDS,
    MAX_CONFIGURABLE_UPLOAD_BYTES,
    MAX_EMBEDDING_TIMEOUT_SECONDS,
    MAX_PINECONE_OPERATION_TIMEOUT_SECONDS,
    PARSER_FORBIDDEN_ENVIRONMENT_NAMES,
    PARSER_FORBIDDEN_ENVIRONMENT_PREFIXES,
    POSTGRES_SETTING_NAMES,
    PROJECT_ROOT,
    REDIS_SETTING_NAMES,
    ROOT_ENV_FILE,
    ApiSettings,
    DatabaseSettings,
    DenseProviderSettings,
    EmbeddingProviderSettings,
    GenerationProviderSettings,
    LogLevel,
    ObjectStorageProvider,
    ObjectStorageSettings,
    ParserSettings,
    PineconeProviderSettings,
    ProcessingProfileName,
    QueueSettings,
    ResearchProfileName,
    RuntimeEnvironment,
    RuntimeLocation,
    RuntimeSettings,
    S3Settings,
    WorkerSettings,
)

__all__ = (
    "DEFAULT_DENSE_INDEX_CONFIG_VERSION",
    "DEFAULT_EMBEDDING_CONFIG_VERSION",
    "DEFAULT_EMBEDDING_DIMENSIONS",
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_EMBEDDING_TIMEOUT_SECONDS",
    "DEFAULT_MAX_EXTRACTED_CHARACTERS",
    "DEFAULT_MAX_PDF_PAGES",
    "DEFAULT_MAX_UPLOAD_BYTES",
    "DEFAULT_OBJECT_STORAGE_ROOT",
    "DEFAULT_PARSER_CHILD_TIMEOUT_SECONDS",
    "DEFAULT_PARSER_MAX_CONCURRENT_CHILDREN",
    "DEFAULT_PARSER_PORT",
    "DEFAULT_PINECONE_OPERATION_TIMEOUT_SECONDS",
    "DEFAULT_PROCESSING_TIMEOUT_SECONDS",
    "DEFAULT_S3_REGION_NAME",
    "DOCKER_SECRETS_ROOT",
    "FRONTEND_ROOT",
    "M1_MAX_PROVIDER_MONTHLY_BUDGET_MICRO_USD",
    "MAX_CONFIGURABLE_PARSER_CHILD_TIMEOUT_SECONDS",
    "MAX_CONFIGURABLE_PARSER_CONCURRENT_CHILDREN",
    "MAX_CONFIGURABLE_PROCESSING_TIMEOUT_SECONDS",
    "MAX_CONFIGURABLE_UPLOAD_BYTES",
    "MAX_EMBEDDING_TIMEOUT_SECONDS",
    "MAX_PINECONE_OPERATION_TIMEOUT_SECONDS",
    "PARSER_FORBIDDEN_ENVIRONMENT_NAMES",
    "PARSER_FORBIDDEN_ENVIRONMENT_PREFIXES",
    "POSTGRES_SETTING_NAMES",
    "PROJECT_ROOT",
    "REDIS_SETTING_NAMES",
    "ROOT_ENV_FILE",
    "ApiSettings",
    "DatabaseSettings",
    "DenseProviderSettings",
    "EmbeddingProviderSettings",
    "GenerationProviderSettings",
    "LogLevel",
    "ObjectStorageProvider",
    "ObjectStorageSettings",
    "ParserSettings",
    "PineconeProviderSettings",
    "ProcessingProfileName",
    "QueueSettings",
    "ResearchProfileName",
    "RuntimeEnvironment",
    "RuntimeLocation",
    "RuntimeSettings",
    "S3Settings",
    "WorkerSettings",
    "get_api_settings",
    "get_parser_settings",
    "get_worker_settings",
    "load_api_settings",
    "load_database_settings",
    "load_generation_provider_settings",
    "load_parser_settings",
    "load_worker_settings",
)


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

    provider = _configuration_value(
        "INVESTMENT_RESEARCH_OBJECT_STORAGE_PROVIDER",
        file_values,
        "local",
    )
    values: dict[str, object] = {
        "object_storage_provider": provider,
        "object_storage_root": _configuration_value(
            "INVESTMENT_RESEARCH_OBJECT_STORAGE_ROOT",
            file_values,
            str(DEFAULT_OBJECT_STORAGE_ROOT),
        ),
    }

    if provider != "s3":
        endpoint_url = _configuration_value(
            "INVESTMENT_RESEARCH_S3_ENDPOINT_URL",
            file_values,
        )
        access_key_id = _secret_configuration_value(
            "INVESTMENT_RESEARCH_S3_ACCESS_KEY_ID",
            file_values,
        )
        secret_access_key = _secret_configuration_value(
            "INVESTMENT_RESEARCH_S3_SECRET_ACCESS_KEY",
            file_values,
        )
        if any(value is not None for value in (endpoint_url, access_key_id, secret_access_key)):
            raise ValueError("S3 endpoint and credentials require the s3 object-storage provider")
        return values

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


def _dense_provider_runtime_values(
    file_values: dict[str, str | None],
    *,
    embedding_key_name: str,
    pinecone_key_name: str,
) -> dict[str, object] | None:
    """Load one complete role-specific provider pair without sharing credentials."""

    embedding_api_key = _secret_configuration_value(embedding_key_name, file_values)
    pinecone_api_key = _secret_configuration_value(pinecone_key_name, file_values)
    if embedding_api_key is None and pinecone_api_key is None:
        return None

    index_host = _configuration_value(
        "INVESTMENT_RESEARCH_PINECONE_INDEX_HOST",
        file_values,
    )
    configured_values = {
        embedding_key_name: embedding_api_key,
        pinecone_key_name: pinecone_api_key,
        "INVESTMENT_RESEARCH_PINECONE_INDEX_HOST": index_host,
    }
    missing_names = [name for name, value in configured_values.items() if not value]
    if missing_names:
        raise ValueError(
            "dense provider configuration is incomplete; missing: " + ", ".join(missing_names)
        )
    assert embedding_api_key is not None
    assert pinecone_api_key is not None
    assert index_host is not None
    return {
        "embedding": {"api_key": embedding_api_key},
        "pinecone": {
            "api_key": pinecone_api_key,
            "index_host": index_host,
        },
    }


def _generation_provider_runtime_values(
    file_values: dict[str, str | None],
) -> dict[str, object] | None:
    """Load the optional API-only generation credential."""

    api_key = _secret_configuration_value(
        "INVESTMENT_RESEARCH_OPENAI_GENERATION_API_KEY",
        file_values,
    )
    return None if api_key is None else {"api_key": api_key}


def _provider_budget_runtime_values(file_values: dict[str, str | None]) -> dict[str, object]:
    """Load the shared workspace budget applied by every paid-provider process."""

    return {
        "provider_monthly_budget_micro_usd": _configuration_value(
            "INVESTMENT_RESEARCH_PROVIDER_MONTHLY_BUDGET_MICRO_USD",
            file_values,
        )
    }


def load_database_settings(*, env_file: Path | None = ROOT_ENV_FILE) -> DatabaseSettings:
    """Load only values required for a direct PostgreSQL maintenance process."""

    return DatabaseSettings.model_validate(_database_runtime_values(_read_local_values(env_file)))


def load_generation_provider_settings(
    *,
    env_file: Path | None = ROOT_ENV_FILE,
) -> GenerationProviderSettings:
    """Load only the API-role generation credential for an explicit provider operation."""

    values = _generation_provider_runtime_values(_read_local_values(env_file))
    if values is None:
        raise ValueError("generation provider configuration is required")
    return GenerationProviderSettings.model_validate(values)


def load_api_settings(*, env_file: Path | None = ROOT_ENV_FILE) -> ApiSettings:
    """Load only values required by the FastAPI process."""

    file_values = _read_local_values(env_file)
    database_values = _database_runtime_values(file_values)
    values = {
        **database_values,
        **_queue_runtime_values(file_values, database_values.get("runtime_location")),
        **_object_storage_runtime_values(file_values),
        **_provider_budget_runtime_values(file_values),
        "processing_profile": _configuration_value(
            "INVESTMENT_RESEARCH_PROCESSING_PROFILE",
            file_values,
            "m1",
        ),
        "research_profile": _configuration_value(
            "INVESTMENT_RESEARCH_RESEARCH_PROFILE",
            file_values,
            "m1",
        ),
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
        "query_providers": _dense_provider_runtime_values(
            file_values,
            embedding_key_name="INVESTMENT_RESEARCH_OPENAI_QUERY_API_KEY",
            pinecone_key_name="INVESTMENT_RESEARCH_PINECONE_QUERY_API_KEY",
        ),
        "generation_provider": _generation_provider_runtime_values(file_values),
        "acceptance_dense_index_path": _configuration_value(
            "INVESTMENT_RESEARCH_M1_ACCEPTANCE_DENSE_INDEX_PATH",
            file_values,
        ),
        "local_model_failure_output": _configuration_value(
            "INVESTMENT_RESEARCH_LOCAL_MODEL_FAILURE_OUTPUT",
            file_values,
            "false",
        ),
    }
    return ApiSettings.model_validate(values)


def load_worker_settings(*, env_file: Path | None = ROOT_ENV_FILE) -> WorkerSettings:
    """Load only values required by the Celery worker."""

    file_values = _read_local_values(env_file)
    database_values = _database_runtime_values(file_values)
    values = {
        **database_values,
        **_queue_runtime_values(file_values, database_values.get("runtime_location")),
        **_object_storage_runtime_values(file_values),
        **_provider_budget_runtime_values(file_values),
        "processing_profile": _configuration_value(
            "INVESTMENT_RESEARCH_PROCESSING_PROFILE",
            file_values,
            "m1",
        ),
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
        "indexing_providers": _dense_provider_runtime_values(
            file_values,
            embedding_key_name="INVESTMENT_RESEARCH_OPENAI_DOCUMENT_API_KEY",
            pinecone_key_name="INVESTMENT_RESEARCH_PINECONE_INDEX_API_KEY",
        ),
        "acceptance_dense_index_path": _configuration_value(
            "INVESTMENT_RESEARCH_M1_ACCEPTANCE_DENSE_INDEX_PATH",
            file_values,
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
        "child_timeout_seconds": _configuration_value(
            "INVESTMENT_RESEARCH_PARSER_CHILD_TIMEOUT_SECONDS",
            file_values,
            str(DEFAULT_PARSER_CHILD_TIMEOUT_SECONDS),
        ),
        "max_concurrent_children": _configuration_value(
            "INVESTMENT_RESEARCH_PARSER_MAX_CONCURRENT_CHILDREN",
            file_values,
            str(DEFAULT_PARSER_MAX_CONCURRENT_CHILDREN),
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
