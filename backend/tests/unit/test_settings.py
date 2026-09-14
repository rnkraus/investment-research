"""Tests for process-specific runtime settings."""

from os import environ
from pathlib import Path

import pytest
from pydantic import ValidationError

from investment_research.modules.processing.contracts import (
    MAX_CONFIGURABLE_EXTRACTED_CHARACTERS,
    MAX_CONFIGURABLE_PDF_PAGES,
)
from investment_research.settings import (
    MAX_CONFIGURABLE_PROCESSING_MEMORY_LIMIT_BYTES,
    MAX_CONFIGURABLE_PROCESSING_TIMEOUT_SECONDS,
    MIN_PROCESSING_MEMORY_LIMIT_BYTES,
    PARSER_FORBIDDEN_ENVIRONMENT_NAMES,
    PARSER_FORBIDDEN_ENVIRONMENT_PREFIXES,
    PROJECT_ROOT,
    ApiSettings,
    ParserSettings,
    WorkerSettings,
    load_api_settings,
    load_database_settings,
    load_parser_settings,
    load_worker_settings,
)

LOCAL_DATABASE_URL = "postgresql+psycopg://test-user:test-password@127.0.0.1:5432/test-database"
CONTAINER_DATABASE_URL = "postgresql+psycopg://test-user:test-password@postgres:5432/test-database"
POSTGRES_SETTING_NAMES = (
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_PORT",
)
type ObjectStorageSettingsFactory = type[ApiSettings] | type[WorkerSettings]
type ParserLimitSettingsFactory = type[WorkerSettings] | type[ParserSettings]


def clear_parser_forbidden_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove host-specific credential variables from one parser-loader test."""

    for name in tuple(environ):
        if name in PARSER_FORBIDDEN_ENVIRONMENT_NAMES or name.startswith(
            PARSER_FORBIDDEN_ENVIRONMENT_PREFIXES
        ):
            monkeypatch.delenv(name, raising=False)


def test_api_settings_load_only_supported_environment_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    object_storage_root = tmp_path / "objects"
    monkeypatch.setenv("INVESTMENT_RESEARCH_ENVIRONMENT", "test")
    monkeypatch.setenv("INVESTMENT_RESEARCH_RUNTIME_LOCATION", "host")
    monkeypatch.setenv("INVESTMENT_RESEARCH_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("INVESTMENT_RESEARCH_API_HOST", "localhost")
    monkeypatch.setenv("INVESTMENT_RESEARCH_API_PORT", "8123")
    monkeypatch.setenv("INVESTMENT_RESEARCH_DATABASE_URL", LOCAL_DATABASE_URL)
    monkeypatch.setenv("INVESTMENT_RESEARCH_OBJECT_STORAGE_ROOT", str(object_storage_root))
    monkeypatch.setenv("INVESTMENT_RESEARCH_MAX_UPLOAD_BYTES", "1048576")

    settings = load_api_settings(env_file=None)

    assert settings.environment == "test"
    assert settings.runtime_location == "host"
    assert settings.log_level == "DEBUG"
    assert settings.api_host == "localhost"
    assert settings.api_port == 8123
    assert settings.require_database_url() == LOCAL_DATABASE_URL
    assert settings.object_storage_root == object_storage_root
    assert settings.max_upload_bytes == 1_048_576
    assert "test-password" not in repr(settings)


def test_worker_settings_load_only_supported_environment_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    object_storage_root = tmp_path / "objects"
    monkeypatch.setenv("INVESTMENT_RESEARCH_ENVIRONMENT", "test")
    monkeypatch.setenv("INVESTMENT_RESEARCH_RUNTIME_LOCATION", "host")
    monkeypatch.setenv("INVESTMENT_RESEARCH_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("INVESTMENT_RESEARCH_DATABASE_URL", LOCAL_DATABASE_URL)
    monkeypatch.setenv("INVESTMENT_RESEARCH_OBJECT_STORAGE_ROOT", str(object_storage_root))
    monkeypatch.setenv("INVESTMENT_RESEARCH_MAX_PDF_PAGES", "100")
    monkeypatch.setenv("INVESTMENT_RESEARCH_MAX_EXTRACTED_CHARACTERS", "1500000")
    monkeypatch.setenv("INVESTMENT_RESEARCH_PROCESSING_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("INVESTMENT_RESEARCH_PROCESSING_MEMORY_LIMIT_BYTES", "268435456")

    settings = load_worker_settings(env_file=None)

    assert settings.environment == "test"
    assert settings.runtime_location == "host"
    assert settings.log_level == "WARNING"
    assert settings.require_database_url() == LOCAL_DATABASE_URL
    assert settings.object_storage_root == object_storage_root
    assert settings.max_pdf_pages == 100
    assert settings.max_extracted_characters == 1_500_000
    assert settings.processing_timeout_seconds == 45
    assert settings.processing_memory_limit_bytes == 256 * 1024 * 1024
    assert "test-password" not in repr(settings)


def test_database_settings_ignore_unowned_api_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INVESTMENT_RESEARCH_DATABASE_URL", LOCAL_DATABASE_URL)
    monkeypatch.setenv("INVESTMENT_RESEARCH_API_HOST", "public.example.invalid")

    settings = load_database_settings(env_file=None)

    assert settings.require_database_url() == LOCAL_DATABASE_URL
    assert "api_host" not in type(settings).model_fields


@pytest.mark.parametrize("settings_factory", [ApiSettings, WorkerSettings])
def test_settings_resolve_relative_object_storage_root_from_project_root(
    settings_factory: ObjectStorageSettingsFactory,
) -> None:
    settings = settings_factory(object_storage_root=Path("output/test-object-storage"))

    assert settings.object_storage_root == PROJECT_ROOT / "output" / "test-object-storage"


@pytest.mark.parametrize("settings_factory", [ApiSettings, WorkerSettings])
def test_settings_reject_object_storage_inside_frontend(
    settings_factory: ObjectStorageSettingsFactory,
) -> None:
    with pytest.raises(ValidationError, match="outside the frontend web root"):
        settings_factory(object_storage_root=PROJECT_ROOT / "frontend" / "uploads")


@pytest.mark.parametrize("max_upload_bytes", [0, 101 * 1024 * 1024])
def test_api_settings_reject_unsafe_upload_limit(max_upload_bytes: int) -> None:
    with pytest.raises(ValidationError, match="max_upload_bytes"):
        ApiSettings(max_upload_bytes=max_upload_bytes)


@pytest.mark.parametrize("settings_factory", [WorkerSettings, ParserSettings])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("max_pdf_pages", 0, id="zero-pages"),
        pytest.param(
            "max_pdf_pages",
            MAX_CONFIGURABLE_PDF_PAGES + 1,
            id="too-many-pages",
        ),
        pytest.param("max_extracted_characters", 0, id="zero-characters"),
        pytest.param(
            "max_extracted_characters",
            MAX_CONFIGURABLE_EXTRACTED_CHARACTERS + 1,
            id="too-many-characters",
        ),
    ],
)
def test_processing_settings_reject_unsafe_parser_limit(
    settings_factory: ParserLimitSettingsFactory,
    field: str,
    value: int,
) -> None:
    with pytest.raises(ValidationError) as error:
        settings_factory.model_validate({field: value})

    assert error.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("processing_timeout_seconds", 0, id="zero-seconds"),
        pytest.param(
            "processing_timeout_seconds",
            MAX_CONFIGURABLE_PROCESSING_TIMEOUT_SECONDS + 1,
            id="excessive-seconds",
        ),
        pytest.param(
            "processing_memory_limit_bytes",
            MIN_PROCESSING_MEMORY_LIMIT_BYTES - 1,
            id="insufficient-memory",
        ),
        pytest.param(
            "processing_memory_limit_bytes",
            MAX_CONFIGURABLE_PROCESSING_MEMORY_LIMIT_BYTES + 1,
            id="excessive-memory",
        ),
    ],
)
def test_worker_settings_reject_unsafe_execution_limit(field: str, value: int) -> None:
    with pytest.raises(ValidationError) as error:
        WorkerSettings.model_validate({field: value})

    assert error.value.errors()[0]["loc"] == (field,)


def test_api_settings_build_database_url_from_local_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in ("INVESTMENT_RESEARCH_DATABASE_URL", *POSTGRES_SETTING_NAMES):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "POSTGRES_DB=research archive",
                "POSTGRES_USER=local researcher",
                "POSTGRES_PASSWORD='test@password/with:symbols'",
                "POSTGRES_PORT=5544",
            )
        ),
        encoding="utf-8",
    )

    settings = load_api_settings(env_file=env_file)

    assert settings.require_database_url() == (
        "postgresql+psycopg://local%20researcher:test%40password%2Fwith%3Asymbols"
        "@127.0.0.1:5544/research%20archive"
    )


def test_process_database_url_overrides_local_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=file-password\n", encoding="utf-8")
    monkeypatch.setenv("INVESTMENT_RESEARCH_DATABASE_URL", LOCAL_DATABASE_URL)

    settings = load_api_settings(env_file=env_file)

    assert settings.require_database_url() == LOCAL_DATABASE_URL


@pytest.mark.parametrize(
    "missing_name",
    ["POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_PORT"],
)
def test_api_settings_reject_incomplete_local_postgres_configuration(
    missing_name: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in ("INVESTMENT_RESEARCH_DATABASE_URL", *POSTGRES_SETTING_NAMES):
        monkeypatch.delenv(name, raising=False)
    values = {
        "POSTGRES_DB": "test-database",
        "POSTGRES_USER": "test-user",
        "POSTGRES_PASSWORD": "test-password",
        "POSTGRES_PORT": "5432",
    }
    del values[missing_name]
    env_file = tmp_path / ".env"
    env_file.write_text(
        "".join(f"{name}={value}\n" for name, value in values.items()),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=missing_name):
        load_api_settings(env_file=env_file)


@pytest.mark.parametrize("postgres_port", ["not-a-number", "0", "65536"])
def test_api_settings_reject_invalid_local_postgres_port(
    postgres_port: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in ("INVESTMENT_RESEARCH_DATABASE_URL", *POSTGRES_SETTING_NAMES):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_DB=test-database\n"
        "POSTGRES_USER=test-user\n"
        "POSTGRES_PASSWORD=test-password\n"
        f"POSTGRES_PORT={postgres_port}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="POSTGRES_PORT"):
        load_api_settings(env_file=env_file)


def test_api_settings_reject_non_loopback_host_runtime_bind() -> None:
    with pytest.raises(ValidationError, match="api_host"):
        ApiSettings(api_host="0.0.0.0")


def test_api_settings_allow_container_runtime_bind() -> None:
    settings = ApiSettings(runtime_location="container", api_host="0.0.0.0")

    assert settings.api_host == "0.0.0.0"


@pytest.mark.parametrize(
    ("database_url", "expected_error"),
    [
        pytest.param(
            "postgresql://test-user:test-password@127.0.0.1/test-database",
            "postgresql\\+psycopg scheme",
            id="wrong-driver",
        ),
        pytest.param(
            "postgresql+psycopg://test-user:test-password@database.internal/test-database",
            "loopback",
            id="remote-host",
        ),
        pytest.param(
            "postgresql+psycopg://test-user@127.0.0.1/test-database",
            "username and password",
            id="missing-password",
        ),
        pytest.param(
            "postgresql+psycopg://test-user:test-password@127.0.0.1/",
            "database name",
            id="missing-database",
        ),
        pytest.param(
            "postgresql+psycopg://test-user:test-password@127.0.0.1:0/test-database",
            "port",
            id="invalid-port",
        ),
        pytest.param(
            "postgresql+psycopg://test-user:test-password@127.0.0.1/test-database#fragment",
            "fragment",
            id="fragment",
        ),
        pytest.param(
            "postgresql+psycopg://test-user:test-password@127.0.0.1/test-database"
            "?host=db.example.invalid",
            "query parameters",
            id="query-host-override",
        ),
    ],
)
def test_database_settings_reject_unsafe_host_url(
    database_url: str,
    expected_error: str,
) -> None:
    with pytest.raises(ValidationError, match=expected_error):
        ApiSettings.model_validate({"database_url": database_url})


def test_database_settings_allow_only_compose_postgres_in_container() -> None:
    settings = ApiSettings.model_validate(
        {
            "runtime_location": "container",
            "api_host": "0.0.0.0",
            "database_url": CONTAINER_DATABASE_URL,
        }
    )

    assert settings.require_database_url() == CONTAINER_DATABASE_URL


@pytest.mark.parametrize(
    "database_url",
    [
        LOCAL_DATABASE_URL,
        "postgresql+psycopg://test-user:test-password@database:5432/test-database",
    ],
)
def test_database_settings_reject_other_targets_in_container(database_url: str) -> None:
    with pytest.raises(ValidationError, match="Compose postgres service"):
        ApiSettings.model_validate(
            {
                "runtime_location": "container",
                "api_host": "0.0.0.0",
                "database_url": database_url,
            }
        )


def test_container_loader_rejects_postgres_bootstrap_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("INVESTMENT_RESEARCH_DATABASE_URL", raising=False)
    monkeypatch.setenv("INVESTMENT_RESEARCH_RUNTIME_LOCATION", "container")
    monkeypatch.setenv("INVESTMENT_RESEARCH_API_HOST", "0.0.0.0")
    monkeypatch.setenv("POSTGRES_DB", "test-database")
    monkeypatch.setenv("POSTGRES_USER", "test-user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "test-password")
    monkeypatch.setenv("POSTGRES_PORT", "5432")

    with pytest.raises(ValueError, match="require INVESTMENT_RESEARCH_DATABASE_URL"):
        load_api_settings(env_file=None)


def test_database_settings_require_url_for_database_access() -> None:
    with pytest.raises(RuntimeError, match="PostgreSQL configuration"):
        ApiSettings().require_database_url()


def test_process_models_expose_only_their_owned_configuration() -> None:
    assert "database_url" in ApiSettings.model_fields
    assert "database_url" in WorkerSettings.model_fields
    assert "database_url" not in ParserSettings.model_fields
    assert "app_name" in ApiSettings.model_fields
    assert "app_name" not in WorkerSettings.model_fields
    assert "app_name" not in ParserSettings.model_fields
    assert "api_host" not in WorkerSettings.model_fields
    assert "api_host" not in ParserSettings.model_fields
    assert "processing_timeout_seconds" not in ApiSettings.model_fields
    assert "processing_timeout_seconds" not in ParserSettings.model_fields


@pytest.mark.parametrize("settings_factory", [ApiSettings, WorkerSettings, ParserSettings])
def test_runtime_settings_reject_unknown_log_level(
    settings_factory: type[ApiSettings] | type[WorkerSettings] | type[ParserSettings],
) -> None:
    with pytest.raises(ValidationError, match="log_level"):
        settings_factory.model_validate({"log_level": "VERBOSE"})


def test_parser_settings_load_non_secret_process_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_parser_forbidden_environment(monkeypatch)
    monkeypatch.setenv("INVESTMENT_RESEARCH_ENVIRONMENT", "test")
    monkeypatch.setenv("INVESTMENT_RESEARCH_RUNTIME_LOCATION", "container")
    monkeypatch.setenv("INVESTMENT_RESEARCH_LOG_LEVEL", "ERROR")
    monkeypatch.setenv("INVESTMENT_RESEARCH_PARSER_HOST", "0.0.0.0")
    monkeypatch.setenv("INVESTMENT_RESEARCH_PARSER_PORT", "8124")
    monkeypatch.setenv("INVESTMENT_RESEARCH_MAX_PDF_PAGES", "80")
    monkeypatch.setenv("INVESTMENT_RESEARCH_MAX_EXTRACTED_CHARACTERS", "500000")

    settings = load_parser_settings()

    assert settings.environment == "test"
    assert settings.runtime_location == "container"
    assert settings.log_level == "ERROR"
    assert settings.parser_host == "0.0.0.0"
    assert settings.parser_port == 8124
    assert settings.max_pdf_pages == 80
    assert settings.max_extracted_characters == 500_000


@pytest.mark.parametrize(
    "forbidden_name",
    [
        "INVESTMENT_RESEARCH_DATABASE_URL",
        "POSTGRES_PASSWORD",
        "MINIO_ROOT_PASSWORD",
        "AWS_SECRET_ACCESS_KEY",
        "OPENAI_API_KEY",
        "LANGSMITH_API_KEY",
    ],
)
def test_parser_settings_reject_application_credentials(
    forbidden_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_parser_forbidden_environment(monkeypatch)
    marker = "synthetic-secret-value"
    monkeypatch.setenv(forbidden_name, marker)

    with pytest.raises(ValueError, match=forbidden_name) as error:
        load_parser_settings()

    assert marker not in str(error.value)


def test_parser_settings_reject_public_bind_on_host() -> None:
    with pytest.raises(ValidationError, match="parser_host"):
        ParserSettings(parser_host="0.0.0.0")
