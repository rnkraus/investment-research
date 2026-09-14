"""Tests for process-specific runtime settings."""

from collections.abc import Callable
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
    S3Settings,
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
S3_SETTING_NAMES = (
    "INVESTMENT_RESEARCH_OBJECT_STORAGE_PROVIDER",
    "INVESTMENT_RESEARCH_S3_ENDPOINT_URL",
    "INVESTMENT_RESEARCH_S3_REGION_NAME",
    "INVESTMENT_RESEARCH_S3_BUCKET",
    "INVESTMENT_RESEARCH_S3_ACCESS_KEY_ID",
    "INVESTMENT_RESEARCH_S3_SECRET_ACCESS_KEY",
)
type ObjectStorageSettingsFactory = type[ApiSettings] | type[WorkerSettings]
type ObjectStorageSettingsLoader = Callable[..., ApiSettings | WorkerSettings]
type ParserLimitSettingsFactory = type[WorkerSettings] | type[ParserSettings]


def clear_parser_forbidden_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove host-specific credential variables from one parser-loader test."""

    for name in tuple(environ):
        if name in PARSER_FORBIDDEN_ENVIRONMENT_NAMES or name.startswith(
            PARSER_FORBIDDEN_ENVIRONMENT_PREFIXES
        ):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def clear_object_storage_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every settings test independent of host S3 configuration."""

    for name in S3_SETTING_NAMES:
        monkeypatch.delenv(name, raising=False)


def valid_s3_configuration(
    *,
    endpoint_url: str = "http://127.0.0.1:8333",
    bucket: str = "investment-research-originals",
    access_key_id: str = "synthetic-application-key",
    secret_access_key: str = "synthetic-application-secret",
) -> dict[str, str]:
    """Return one complete synthetic S3 connection configuration."""

    return {
        "endpoint_url": endpoint_url,
        "region_name": "us-east-1",
        "bucket": bucket,
        "access_key_id": access_key_id,
        "secret_access_key": secret_access_key,
    }


def valid_s3_values(**overrides: str) -> dict[str, object]:
    """Return complete provider values with optional S3 field overrides."""

    return {
        "object_storage_provider": "s3",
        "s3": valid_s3_configuration(**overrides),
    }


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


@pytest.mark.parametrize("loader", [load_api_settings, load_worker_settings])
def test_object_storage_settings_load_complete_s3_configuration(
    loader: ObjectStorageSettingsLoader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "synthetic-application-secret"
    monkeypatch.setenv("INVESTMENT_RESEARCH_OBJECT_STORAGE_PROVIDER", "s3")
    monkeypatch.setenv("INVESTMENT_RESEARCH_S3_ENDPOINT_URL", "http://127.0.0.1:8333")
    monkeypatch.setenv("INVESTMENT_RESEARCH_S3_REGION_NAME", "us-east-1")
    monkeypatch.setenv("INVESTMENT_RESEARCH_S3_BUCKET", "investment-research-originals")
    monkeypatch.setenv("INVESTMENT_RESEARCH_S3_ACCESS_KEY_ID", "synthetic-application-key")
    monkeypatch.setenv("INVESTMENT_RESEARCH_S3_SECRET_ACCESS_KEY", marker)

    settings = loader(env_file=None)
    s3 = settings.require_s3_settings()

    assert s3.endpoint_url == "http://127.0.0.1:8333"
    assert s3.region_name == "us-east-1"
    assert s3.bucket == "investment-research-originals"
    assert s3.access_key_id.get_secret_value() == "synthetic-application-key"
    assert s3.secret_access_key.get_secret_value() == marker
    assert marker not in repr(settings)


@pytest.mark.parametrize("settings_factory", [ApiSettings, WorkerSettings])
def test_s3_settings_allow_only_compose_gateway_in_container(
    settings_factory: ObjectStorageSettingsFactory,
) -> None:
    settings = settings_factory.model_validate(
        {
            "runtime_location": "container",
            **valid_s3_values(endpoint_url="http://seaweedfs-s3:8333"),
        }
    )

    assert settings.require_s3_settings().endpoint_url == "http://seaweedfs-s3:8333"


@pytest.mark.parametrize("settings_factory", [ApiSettings, WorkerSettings])
@pytest.mark.parametrize(
    ("endpoint_url", "expected_error"),
    [
        pytest.param("ftp://127.0.0.1:8333", "HTTP or HTTPS", id="wrong-scheme"),
        pytest.param(
            "http://user:password@127.0.0.1:8333",
            "must not contain credentials",
            id="embedded-credentials",
        ),
        pytest.param(
            "http://127.0.0.1:8333/bucket",
            "path, query, or fragment",
            id="path",
        ),
        pytest.param(
            "http://127.0.0.1:8333?bucket=private",
            "path, query, or fragment",
            id="query",
        ),
        pytest.param(
            "http://127.0.0.1:8333#private",
            "path, query, or fragment",
            id="fragment",
        ),
        pytest.param(
            "http://storage.example.invalid:8333",
            "loopback",
            id="remote-host",
        ),
    ],
)
def test_s3_settings_reject_unsafe_host_endpoint(
    settings_factory: ObjectStorageSettingsFactory,
    endpoint_url: str,
    expected_error: str,
) -> None:
    with pytest.raises(ValidationError, match=expected_error):
        settings_factory.model_validate(valid_s3_values(endpoint_url=endpoint_url))


@pytest.mark.parametrize("settings_factory", [ApiSettings, WorkerSettings])
def test_s3_settings_reject_other_container_target(
    settings_factory: ObjectStorageSettingsFactory,
) -> None:
    with pytest.raises(ValidationError, match="Compose seaweedfs-s3 service"):
        settings_factory.model_validate(
            {
                "runtime_location": "container",
                **valid_s3_values(endpoint_url="http://seaweedfs-master:9333"),
            }
        )


@pytest.mark.parametrize("settings_factory", [ApiSettings, WorkerSettings])
def test_s3_provider_requires_complete_s3_configuration(
    settings_factory: ObjectStorageSettingsFactory,
) -> None:
    with pytest.raises(ValidationError, match="s3 configuration is required"):
        settings_factory(object_storage_provider="s3")


@pytest.mark.parametrize("settings_factory", [ApiSettings, WorkerSettings])
@pytest.mark.parametrize(
    "missing_field",
    ["endpoint_url", "bucket", "access_key_id", "secret_access_key"],
)
def test_s3_provider_rejects_each_missing_required_field(
    settings_factory: ObjectStorageSettingsFactory,
    missing_field: str,
) -> None:
    s3 = valid_s3_configuration()
    del s3[missing_field]

    with pytest.raises(ValidationError) as error:
        settings_factory.model_validate({"object_storage_provider": "s3", "s3": s3})

    assert error.value.errors()[0]["loc"] == ("s3", missing_field)


@pytest.mark.parametrize("settings_factory", [ApiSettings, WorkerSettings])
def test_local_provider_rejects_unused_s3_configuration(
    settings_factory: ObjectStorageSettingsFactory,
) -> None:
    with pytest.raises(ValidationError, match="not allowed for the local provider"):
        settings_factory.model_validate(
            {"object_storage_provider": "local", "s3": valid_s3_configuration()}
        )


@pytest.mark.parametrize(
    "bucket",
    ["ab", "UPPERCASE", "invalid_bucket", "192.168.0.1", "adjacent..dots"],
)
def test_s3_settings_reject_non_portable_bucket_names(bucket: str) -> None:
    with pytest.raises(ValidationError, match="bucket"):
        S3Settings.model_validate(valid_s3_configuration(bucket=bucket))


@pytest.mark.parametrize("field", ["access_key_id", "secret_access_key"])
def test_s3_settings_reject_blank_or_padded_credentials(field: str) -> None:
    values = valid_s3_configuration()
    marker = "synthetic-invalid-secret"
    values[field] = f" {marker} "

    with pytest.raises(ValidationError, match="normalized") as error:
        S3Settings.model_validate(values)

    assert marker not in str(error.value)


def test_local_provider_does_not_expose_s3_settings() -> None:
    with pytest.raises(RuntimeError, match="S3 configuration"):
        ApiSettings().require_s3_settings()


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
    assert "s3" in ApiSettings.model_fields
    assert "s3" in WorkerSettings.model_fields
    assert "s3" not in ParserSettings.model_fields
    assert "object_storage_provider" not in ParserSettings.model_fields
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
        "INVESTMENT_RESEARCH_S3_SECRET_ACCESS_KEY",
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
