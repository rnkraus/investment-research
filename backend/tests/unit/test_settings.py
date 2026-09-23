"""Tests for process-specific runtime settings."""

from collections.abc import Callable
from os import environ
from pathlib import Path

import pytest
from pydantic import ValidationError

from investment_research import settings as settings_module
from investment_research.modules.processing.contracts import (
    MAX_CONFIGURABLE_EXTRACTED_CHARACTERS,
    MAX_CONFIGURABLE_PDF_PAGES,
)
from investment_research.settings import (
    MAX_CONFIGURABLE_PARSER_CHILD_TIMEOUT_SECONDS,
    MAX_CONFIGURABLE_PARSER_CONCURRENT_CHILDREN,
    MAX_CONFIGURABLE_PROCESSING_TIMEOUT_SECONDS,
    PARSER_FORBIDDEN_ENVIRONMENT_NAMES,
    PARSER_FORBIDDEN_ENVIRONMENT_PREFIXES,
    PROJECT_ROOT,
    ApiSettings,
    DenseProviderSettings,
    EmbeddingProviderSettings,
    GenerationProviderSettings,
    ParserSettings,
    PineconeProviderSettings,
    S3Settings,
    WorkerSettings,
    load_api_settings,
    load_database_settings,
    load_generation_provider_settings,
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
POSTGRES_FILE_SETTING_NAMES = (
    "INVESTMENT_RESEARCH_DATABASE_URL_FILE",
    "POSTGRES_PASSWORD_FILE",
)
S3_SETTING_NAMES = (
    "INVESTMENT_RESEARCH_OBJECT_STORAGE_PROVIDER",
    "INVESTMENT_RESEARCH_S3_ENDPOINT_URL",
    "INVESTMENT_RESEARCH_S3_REGION_NAME",
    "INVESTMENT_RESEARCH_S3_BUCKET",
    "INVESTMENT_RESEARCH_S3_ACCESS_KEY_ID",
    "INVESTMENT_RESEARCH_S3_ACCESS_KEY_ID_FILE",
    "INVESTMENT_RESEARCH_S3_SECRET_ACCESS_KEY",
    "INVESTMENT_RESEARCH_S3_SECRET_ACCESS_KEY_FILE",
)
QUEUE_SETTING_NAMES = (
    "INVESTMENT_RESEARCH_REDIS_URL",
    "INVESTMENT_RESEARCH_REDIS_URL_FILE",
    "REDIS_PASSWORD",
    "REDIS_PASSWORD_FILE",
    "REDIS_PORT",
)
PROVIDER_SETTING_NAMES = (
    "INVESTMENT_RESEARCH_PROVIDER_MONTHLY_BUDGET_MICRO_USD",
    "INVESTMENT_RESEARCH_M1_ACCEPTANCE_DENSE_INDEX_PATH",
    "INVESTMENT_RESEARCH_OPENAI_QUERY_API_KEY",
    "INVESTMENT_RESEARCH_OPENAI_QUERY_API_KEY_FILE",
    "INVESTMENT_RESEARCH_PINECONE_QUERY_API_KEY",
    "INVESTMENT_RESEARCH_PINECONE_QUERY_API_KEY_FILE",
    "INVESTMENT_RESEARCH_OPENAI_GENERATION_API_KEY",
    "INVESTMENT_RESEARCH_OPENAI_GENERATION_API_KEY_FILE",
    "INVESTMENT_RESEARCH_OPENAI_DOCUMENT_API_KEY",
    "INVESTMENT_RESEARCH_OPENAI_DOCUMENT_API_KEY_FILE",
    "INVESTMENT_RESEARCH_PINECONE_INDEX_API_KEY",
    "INVESTMENT_RESEARCH_PINECONE_INDEX_API_KEY_FILE",
    "INVESTMENT_RESEARCH_PINECONE_INDEX_HOST",
)
type ObjectStorageSettingsFactory = type[ApiSettings] | type[WorkerSettings]
type ObjectStorageSettingsLoader = Callable[..., ApiSettings | WorkerSettings]
type ParserLimitSettingsFactory = type[WorkerSettings] | type[ParserSettings]
type QueueSettingsFactory = type[ApiSettings] | type[WorkerSettings]


def clear_parser_forbidden_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove host-specific credential variables from one parser-loader test."""

    for name in tuple(environ):
        if name in PARSER_FORBIDDEN_ENVIRONMENT_NAMES or name.startswith(
            PARSER_FORBIDDEN_ENVIRONMENT_PREFIXES
        ):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def clear_external_service_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep settings tests independent of host database, queue, and S3 configuration."""

    for name in (
        "INVESTMENT_RESEARCH_DATABASE_URL",
        *POSTGRES_SETTING_NAMES,
        *POSTGRES_FILE_SETTING_NAMES,
        *S3_SETTING_NAMES,
        *QUEUE_SETTING_NAMES,
        *PROVIDER_SETTING_NAMES,
    ):
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
    monkeypatch.setenv(
        "INVESTMENT_RESEARCH_REDIS_URL",
        "redis://:synthetic-redis-secret@127.0.0.1:6379/0",
    )
    monkeypatch.setenv("INVESTMENT_RESEARCH_OBJECT_STORAGE_ROOT", str(object_storage_root))
    monkeypatch.setenv("INVESTMENT_RESEARCH_MAX_UPLOAD_BYTES", "1048576")

    settings = load_api_settings(env_file=None)

    assert settings.environment == "test"
    assert settings.runtime_location == "host"
    assert settings.log_level == "DEBUG"
    assert settings.api_host == "localhost"
    assert settings.api_port == 8123
    assert settings.require_database_url() == LOCAL_DATABASE_URL
    assert settings.require_redis_url() == ("redis://:synthetic-redis-secret@127.0.0.1:6379/0")
    assert settings.object_storage_root == object_storage_root
    assert settings.max_upload_bytes == 1_048_576
    assert "test-password" not in repr(settings)


def test_local_api_ignores_compose_only_s3_region_and_bucket(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Allow one shared development file without activating S3 for the host API."""

    env_file = tmp_path / "development.env"
    env_file.write_text(
        "INVESTMENT_RESEARCH_OBJECT_STORAGE_PROVIDER=local\n"
        "INVESTMENT_RESEARCH_OBJECT_STORAGE_ROOT=output/object-storage\n"
        "INVESTMENT_RESEARCH_S3_REGION_NAME=us-east-1\n"
        "INVESTMENT_RESEARCH_S3_BUCKET=investment-research-local\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("INVESTMENT_RESEARCH_DATABASE_URL", LOCAL_DATABASE_URL)
    monkeypatch.setenv(
        "INVESTMENT_RESEARCH_REDIS_URL",
        "redis://:synthetic-redis-secret@127.0.0.1:6379/0",
    )

    settings = load_api_settings(env_file=env_file)

    assert settings.object_storage_provider == "local"
    assert settings.s3 is None


def test_worker_settings_load_only_supported_environment_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    object_storage_root = tmp_path / "objects"
    monkeypatch.setenv("INVESTMENT_RESEARCH_ENVIRONMENT", "test")
    monkeypatch.setenv("INVESTMENT_RESEARCH_RUNTIME_LOCATION", "host")
    monkeypatch.setenv("INVESTMENT_RESEARCH_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("INVESTMENT_RESEARCH_DATABASE_URL", LOCAL_DATABASE_URL)
    monkeypatch.setenv(
        "INVESTMENT_RESEARCH_REDIS_URL",
        "redis://:synthetic-redis-secret@localhost:6379/0",
    )
    monkeypatch.setenv("INVESTMENT_RESEARCH_OBJECT_STORAGE_ROOT", str(object_storage_root))
    monkeypatch.setenv("INVESTMENT_RESEARCH_PARSER_ENDPOINT_URL", "http://localhost:8100")
    monkeypatch.setenv("INVESTMENT_RESEARCH_MAX_UPLOAD_BYTES", "1048576")
    monkeypatch.setenv("INVESTMENT_RESEARCH_MAX_PDF_PAGES", "100")
    monkeypatch.setenv("INVESTMENT_RESEARCH_MAX_EXTRACTED_CHARACTERS", "1500000")
    monkeypatch.setenv("INVESTMENT_RESEARCH_PROCESSING_TIMEOUT_SECONDS", "45")

    settings = load_worker_settings(env_file=None)

    assert settings.environment == "test"
    assert settings.runtime_location == "host"
    assert settings.log_level == "WARNING"
    assert settings.require_database_url() == LOCAL_DATABASE_URL
    assert settings.require_redis_url() == ("redis://:synthetic-redis-secret@localhost:6379/0")
    assert settings.object_storage_root == object_storage_root
    assert settings.require_parser_endpoint_url() == "http://localhost:8100"
    assert settings.max_parser_request_bytes == 1_048_576
    assert settings.max_pdf_pages == 100
    assert settings.max_extracted_characters == 1_500_000
    assert settings.processing_timeout_seconds == 45
    assert "test-password" not in repr(settings)
    assert "synthetic-redis-secret" not in repr(settings)


@pytest.mark.parametrize("settings_factory", [ApiSettings, WorkerSettings])
def test_queue_settings_allow_only_compose_redis_service_in_container(
    settings_factory: QueueSettingsFactory,
) -> None:
    settings = settings_factory.model_validate(
        {
            "runtime_location": "container",
            "redis_url": "redis://:synthetic-secret@redis:6379/0",
        }
    )

    assert settings.require_redis_url() == "redis://:synthetic-secret@redis:6379/0"


@pytest.mark.parametrize("settings_factory", [ApiSettings, WorkerSettings])
@pytest.mark.parametrize(
    ("redis_url", "expected_error"),
    [
        pytest.param(
            "rediss://:synthetic-secret@127.0.0.1:6379/0",
            "redis scheme",
            id="wrong-scheme",
        ),
        pytest.param(
            "redis://:synthetic-secret@cache.example.invalid:6379/0",
            "loopback",
            id="remote-host",
        ),
        pytest.param(
            "redis://default:synthetic-secret@127.0.0.1:6379/0",
            "password-only",
            id="username",
        ),
        pytest.param(
            "redis://127.0.0.1:6379/0",
            "include a password",
            id="missing-password",
        ),
        pytest.param(
            "redis://:synthetic-secret@127.0.0.1:6379/1",
            "database /0",
            id="wrong-database",
        ),
        pytest.param(
            "redis://:synthetic-secret@127.0.0.1:6379/0?credential=marker",
            "query parameters",
            id="query",
        ),
        pytest.param(
            "redis://:synthetic-secret@127.0.0.1:6379/0#marker",
            "fragment",
            id="fragment",
        ),
    ],
)
def test_queue_settings_reject_unsafe_broker_url(
    settings_factory: QueueSettingsFactory,
    redis_url: str,
    expected_error: str,
) -> None:
    with pytest.raises(ValidationError, match=expected_error) as error:
        settings_factory.model_validate({"redis_url": redis_url})

    assert "synthetic-secret" not in str(error.value)


def test_queue_settings_require_configuration_only_when_queue_is_used() -> None:
    with pytest.raises(RuntimeError, match="Redis configuration"):
        ApiSettings().require_redis_url()


@pytest.mark.parametrize("loader", [load_api_settings, load_worker_settings])
def test_queue_settings_build_url_from_local_environment(
    loader: ObjectStorageSettingsLoader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDIS_PASSWORD", "synthetic@password/with:symbols")
    monkeypatch.setenv("REDIS_PORT", "6380")

    settings = loader(env_file=None)

    assert settings.require_redis_url() == (
        "redis://:synthetic%40password%2Fwith%3Asymbols@127.0.0.1:6380/0"
    )


@pytest.mark.parametrize("loader", [load_api_settings, load_worker_settings])
def test_queue_settings_reject_incomplete_local_environment(
    loader: ObjectStorageSettingsLoader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDIS_PASSWORD", "synthetic-secret")

    with pytest.raises(ValueError, match="missing: REDIS_PORT"):
        loader(env_file=None)


def test_worker_settings_build_container_connections_from_mounted_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings_module, "DOCKER_SECRETS_ROOT", tmp_path)
    postgres_password = tmp_path / "postgres_password"
    redis_password = tmp_path / "redis_password"
    s3_access_key = tmp_path / "s3_access_key"
    s3_secret_key = tmp_path / "s3_secret_key"
    postgres_password.write_text("postgres@secret/one", encoding="utf-8")
    redis_password.write_text("redis@secret/two", encoding="utf-8")
    s3_access_key.write_text("synthetic-application-key", encoding="utf-8")
    s3_secret_key.write_text("synthetic-application-secret", encoding="utf-8")

    monkeypatch.setenv("INVESTMENT_RESEARCH_RUNTIME_LOCATION", "container")
    monkeypatch.setenv("POSTGRES_DB", "test database")
    monkeypatch.setenv("POSTGRES_USER", "test user")
    monkeypatch.setenv("POSTGRES_PASSWORD_FILE", str(postgres_password))
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("REDIS_PASSWORD_FILE", str(redis_password))
    monkeypatch.setenv("REDIS_PORT", "6379")
    monkeypatch.setenv("INVESTMENT_RESEARCH_OBJECT_STORAGE_PROVIDER", "s3")
    monkeypatch.setenv("INVESTMENT_RESEARCH_S3_ENDPOINT_URL", "http://seaweedfs-s3:8333")
    monkeypatch.setenv("INVESTMENT_RESEARCH_S3_REGION_NAME", "us-east-1")
    monkeypatch.setenv("INVESTMENT_RESEARCH_S3_BUCKET", "investment-research-originals")
    monkeypatch.setenv("INVESTMENT_RESEARCH_S3_ACCESS_KEY_ID_FILE", str(s3_access_key))
    monkeypatch.setenv("INVESTMENT_RESEARCH_S3_SECRET_ACCESS_KEY_FILE", str(s3_secret_key))
    monkeypatch.setenv("INVESTMENT_RESEARCH_PARSER_ENDPOINT_URL", "http://parser:8100")

    settings = load_worker_settings(env_file=None)

    assert settings.require_database_url() == (
        "postgresql+psycopg://test%20user:postgres%40secret%2Fone@postgres:5432/test%20database"
    )
    assert settings.require_redis_url() == "redis://:redis%40secret%2Ftwo@redis:6379/0"
    assert settings.require_s3_settings().access_key_id.get_secret_value() == (
        "synthetic-application-key"
    )
    assert settings.require_s3_settings().secret_access_key.get_secret_value() == (
        "synthetic-application-secret"
    )


def test_secret_file_rejects_direct_value_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings_module, "DOCKER_SECRETS_ROOT", tmp_path)
    secret = tmp_path / "redis_password"
    secret.write_text("file-secret", encoding="utf-8")
    monkeypatch.setenv("REDIS_PASSWORD", "direct-secret")
    monkeypatch.setenv("REDIS_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("REDIS_PORT", "6379")

    with pytest.raises(ValueError, match="cannot both be configured") as error:
        load_worker_settings(env_file=None)

    assert "direct-secret" not in str(error.value)
    assert "file-secret" not in str(error.value)


def test_secret_file_rejects_path_outside_secret_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    outside = tmp_path / "outside-secret"
    outside.write_text("synthetic-secret", encoding="utf-8")
    monkeypatch.setattr(settings_module, "DOCKER_SECRETS_ROOT", secret_root)
    monkeypatch.setenv("REDIS_PASSWORD_FILE", str(outside))
    monkeypatch.setenv("REDIS_PORT", "6379")

    with pytest.raises(ValueError, match="directly under /run/secrets"):
        load_worker_settings(env_file=None)


@pytest.mark.parametrize("failure", ["missing", "empty", "symlink"])
def test_secret_file_fails_closed_for_unusable_file(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings_module, "DOCKER_SECRETS_ROOT", tmp_path)
    secret = tmp_path / "redis_password"
    if failure == "empty":
        secret.write_text("", encoding="utf-8")
    elif failure == "symlink":
        target = tmp_path / "target"
        target.write_text("synthetic-secret", encoding="utf-8")
        secret.symlink_to(target)
    monkeypatch.setenv("REDIS_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("REDIS_PORT", "6379")

    with pytest.raises(ValueError, match=r"unavailable|empty|symbolic link"):
        load_worker_settings(env_file=None)


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


def test_container_loader_builds_postgres_url_from_bootstrap_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("INVESTMENT_RESEARCH_DATABASE_URL", raising=False)
    monkeypatch.setenv("INVESTMENT_RESEARCH_RUNTIME_LOCATION", "container")
    monkeypatch.setenv("INVESTMENT_RESEARCH_API_HOST", "0.0.0.0")
    monkeypatch.setenv("POSTGRES_DB", "test-database")
    monkeypatch.setenv("POSTGRES_USER", "test-user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "test-password")
    monkeypatch.setenv("POSTGRES_PORT", "5432")

    settings = load_api_settings(env_file=None)

    assert settings.require_database_url() == CONTAINER_DATABASE_URL


def test_database_settings_require_url_for_database_access() -> None:
    with pytest.raises(RuntimeError, match="PostgreSQL configuration"):
        ApiSettings().require_database_url()


def test_dense_provider_settings_freeze_m1_contract_and_hide_secrets() -> None:
    embedding_marker = "synthetic-query-embedding-secret"
    pinecone_marker = "synthetic-query-pinecone-secret"
    providers = DenseProviderSettings(
        embedding=EmbeddingProviderSettings.model_validate({"api_key": embedding_marker}),
        pinecone=PineconeProviderSettings.model_validate(
            {
                "api_key": pinecone_marker,
                "index_host": "synthetic-index.example.svc.pinecone.io",
            }
        ),
    )

    assert providers.embedding.model == "text-embedding-3-small"
    assert providers.embedding.dimensions == 1_536
    assert providers.embedding.config_version == "openai-text-embedding-3-small-1536-v1"
    assert providers.embedding.timeout_seconds == 30
    assert providers.pinecone.config_version == ("pinecone-serverless-cosine-1536-aws-us-east-1-v1")
    assert providers.pinecone.operation_timeout_seconds == 20
    assert embedding_marker not in repr(providers)
    assert pinecone_marker not in repr(providers)

    with pytest.raises(ValidationError, match="model"):
        EmbeddingProviderSettings.model_validate(
            {"api_key": "synthetic-key", "model": "unapproved-model"}
        )
    with pytest.raises(ValidationError, match="index_host"):
        PineconeProviderSettings.model_validate(
            {
                "api_key": "synthetic-key",
                "index_host": "https://synthetic-index.example.svc.pinecone.io/path",
            }
        )


def test_api_and_worker_accept_one_shared_m1_acceptance_index(tmp_path: Path) -> None:
    index_path = tmp_path / "dense-index.sqlite3"

    api = ApiSettings(
        environment="test",
        provider_monthly_budget_micro_usd=5_000_000,
        acceptance_dense_index_path=index_path,
    )
    worker = WorkerSettings(
        environment="test",
        processing_profile="m1",
        provider_monthly_budget_micro_usd=5_000_000,
        acceptance_dense_index_path=index_path,
    )

    assert api.require_acceptance_dense_index_path() == index_path
    assert worker.require_acceptance_dense_index_path() == index_path
    assert worker.require_m1_processing_configuration() == 5_000_000


def test_m1_worker_rejects_missing_indexing_configuration_before_processing() -> None:
    settings = WorkerSettings()

    with pytest.raises(
        ValueError, match="requires indexing providers or the test acceptance index"
    ):
        settings.require_m1_processing_configuration()


@pytest.mark.parametrize(
    ("settings_type", "values", "expected_error"),
    [
        pytest.param(
            ApiSettings,
            {
                "provider_monthly_budget_micro_usd": 5_000_000,
                "acceptance_dense_index_path": "/tmp/dense-index.sqlite3",
            },
            "test environment",
            id="api-development-environment",
        ),
        pytest.param(
            WorkerSettings,
            {
                "environment": "test",
                "provider_monthly_budget_micro_usd": 5_000_000,
                "acceptance_dense_index_path": "relative/dense-index.sqlite3",
            },
            "must be absolute",
            id="worker-relative-path",
        ),
        pytest.param(
            ApiSettings,
            {
                "environment": "test",
                "provider_monthly_budget_micro_usd": 5_000_000,
                "acceptance_dense_index_path": "/run/secrets/dense-index.sqlite3",
            },
            "secrets directory",
            id="api-secret-path",
        ),
        pytest.param(
            WorkerSettings,
            {
                "environment": "test",
                "processing_profile": "m0-regression",
                "provider_monthly_budget_micro_usd": 5_000_000,
                "acceptance_dense_index_path": "/tmp/dense-index.sqlite3",
            },
            "M1 processing profile",
            id="worker-m0-profile",
        ),
        pytest.param(
            ApiSettings,
            {
                "environment": "test",
                "acceptance_dense_index_path": "/tmp/dense-index.sqlite3",
            },
            "monthly budget",
            id="api-missing-budget",
        ),
    ],
)
def test_m1_acceptance_index_rejects_unsafe_configuration(
    settings_type: type[ApiSettings] | type[WorkerSettings],
    values: dict[str, object],
    expected_error: str,
) -> None:
    with pytest.raises(ValidationError, match=expected_error):
        settings_type.model_validate(values)


def test_api_and_worker_load_shared_m1_acceptance_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in PROVIDER_SETTING_NAMES:
        monkeypatch.delenv(name, raising=False)
    index_path = tmp_path / "dense-index.sqlite3"
    monkeypatch.setenv("INVESTMENT_RESEARCH_ENVIRONMENT", "test")
    monkeypatch.setenv(
        "INVESTMENT_RESEARCH_PROVIDER_MONTHLY_BUDGET_MICRO_USD",
        "5000000",
    )
    monkeypatch.setenv(
        "INVESTMENT_RESEARCH_M1_ACCEPTANCE_DENSE_INDEX_PATH",
        str(index_path),
    )

    api = load_api_settings(env_file=None)
    worker = load_worker_settings(env_file=None)

    assert api.acceptance_dense_index_path == index_path
    assert worker.acceptance_dense_index_path == index_path


def test_api_and_worker_load_distinct_least_privilege_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "INVESTMENT_RESEARCH_PROVIDER_MONTHLY_BUDGET_MICRO_USD",
        "5000000",
    )
    monkeypatch.setenv(
        "INVESTMENT_RESEARCH_PINECONE_INDEX_HOST",
        "synthetic-index.example.svc.pinecone.io",
    )
    monkeypatch.setenv(
        "INVESTMENT_RESEARCH_OPENAI_QUERY_API_KEY",
        "synthetic-query-embedding-secret",
    )
    monkeypatch.setenv(
        "INVESTMENT_RESEARCH_PINECONE_QUERY_API_KEY",
        "synthetic-query-pinecone-secret",
    )
    monkeypatch.setenv(
        "INVESTMENT_RESEARCH_OPENAI_GENERATION_API_KEY",
        "synthetic-generation-secret",
    )
    monkeypatch.setenv(
        "INVESTMENT_RESEARCH_OPENAI_DOCUMENT_API_KEY",
        "synthetic-document-embedding-secret",
    )
    monkeypatch.setenv(
        "INVESTMENT_RESEARCH_PINECONE_INDEX_API_KEY",
        "synthetic-index-pinecone-secret",
    )

    api_settings = load_api_settings(env_file=None)
    worker_settings = load_worker_settings(env_file=None)

    query_providers = api_settings.require_query_providers()
    generation_provider = api_settings.require_generation_provider()
    indexing_providers = worker_settings.require_indexing_providers()
    assert api_settings.provider_monthly_budget_micro_usd == 5_000_000
    assert worker_settings.provider_monthly_budget_micro_usd == 5_000_000
    assert worker_settings.require_m1_processing_configuration() == 5_000_000
    assert query_providers.embedding.api_key.get_secret_value() == (
        "synthetic-query-embedding-secret"
    )
    assert query_providers.pinecone.api_key.get_secret_value() == (
        "synthetic-query-pinecone-secret"
    )
    assert generation_provider.api_key.get_secret_value() == "synthetic-generation-secret"
    assert indexing_providers.embedding.api_key.get_secret_value() == (
        "synthetic-document-embedding-secret"
    )
    assert indexing_providers.pinecone.api_key.get_secret_value() == (
        "synthetic-index-pinecone-secret"
    )


def test_generation_provider_hides_and_normalizes_its_api_key() -> None:
    marker = "synthetic-generation-secret"
    settings = GenerationProviderSettings.model_validate({"api_key": marker})

    assert settings.api_key.get_secret_value() == marker
    assert marker not in repr(settings)

    with pytest.raises(ValidationError, match="generation API key") as captured:
        GenerationProviderSettings.model_validate({"api_key": f" {marker}"})

    assert marker not in str(captured.value)


def test_generation_provider_loader_is_role_specific(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "synthetic-generation-secret"
    monkeypatch.setenv("INVESTMENT_RESEARCH_OPENAI_GENERATION_API_KEY", marker)
    monkeypatch.setenv("INVESTMENT_RESEARCH_OPENAI_QUERY_API_KEY", "unused-query-secret")

    settings = load_generation_provider_settings(env_file=None)

    assert settings.api_key.get_secret_value() == marker
    assert marker not in repr(settings)


def test_generation_provider_loader_requires_its_owned_credential() -> None:
    with pytest.raises(ValueError, match="generation provider configuration is required"):
        load_generation_provider_settings(env_file=None)


def test_api_requires_query_and_generation_provider_groups_together() -> None:
    providers = DenseProviderSettings(
        embedding=EmbeddingProviderSettings.model_validate({"api_key": "synthetic-query-key"}),
        pinecone=PineconeProviderSettings.model_validate(
            {
                "api_key": "synthetic-pinecone-key",
                "index_host": "synthetic-index.example.svc.pinecone.io",
            }
        ),
    )
    generation = GenerationProviderSettings.model_validate({"api_key": "synthetic-generation-key"})

    with pytest.raises(ValidationError, match="requires query and generation providers together"):
        ApiSettings(query_providers=providers)
    with pytest.raises(ValidationError, match="requires query and generation providers together"):
        ApiSettings(generation_provider=generation)


def test_paid_provider_settings_require_the_shared_capped_monthly_budget() -> None:
    providers = DenseProviderSettings(
        embedding=EmbeddingProviderSettings.model_validate({"api_key": "synthetic-query-key"}),
        pinecone=PineconeProviderSettings.model_validate(
            {
                "api_key": "synthetic-pinecone-key",
                "index_host": "synthetic-index.example.svc.pinecone.io",
            }
        ),
    )
    generation = GenerationProviderSettings.model_validate({"api_key": "synthetic-generation-key"})

    with pytest.raises(ValidationError, match="requires the monthly provider budget"):
        ApiSettings(query_providers=providers, generation_provider=generation)
    with pytest.raises(ValidationError, match="requires the monthly provider budget"):
        WorkerSettings(indexing_providers=providers)
    with pytest.raises(ValidationError, match="requires the monthly provider budget"):
        ApiSettings(provider_monthly_budget_micro_usd=5_000_000)
    with pytest.raises(ValidationError, match="less than or equal to 5000000"):
        ApiSettings(
            query_providers=providers,
            generation_provider=generation,
            provider_monthly_budget_micro_usd=5_000_001,
        )

    settings = ApiSettings(
        query_providers=providers,
        generation_provider=generation,
        provider_monthly_budget_micro_usd=5_000_000,
    )
    assert settings.provider_monthly_budget_micro_usd == 5_000_000


@pytest.mark.parametrize(
    ("loader", "configured_name", "missing_name"),
    [
        pytest.param(
            load_api_settings,
            "INVESTMENT_RESEARCH_OPENAI_QUERY_API_KEY",
            "INVESTMENT_RESEARCH_PINECONE_QUERY_API_KEY",
            id="api",
        ),
        pytest.param(
            load_worker_settings,
            "INVESTMENT_RESEARCH_OPENAI_DOCUMENT_API_KEY",
            "INVESTMENT_RESEARCH_PINECONE_INDEX_API_KEY",
            id="worker",
        ),
    ],
)
def test_provider_loader_rejects_partial_role_configuration_without_secret_values(
    loader: Callable[..., ApiSettings | WorkerSettings],
    configured_name: str,
    missing_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "synthetic-sensitive-provider-secret"
    monkeypatch.setenv(configured_name, marker)
    monkeypatch.setenv(
        "INVESTMENT_RESEARCH_PINECONE_INDEX_HOST",
        "synthetic-index.example.svc.pinecone.io",
    )

    with pytest.raises(ValueError, match=missing_name) as captured:
        loader(env_file=None)

    assert marker not in str(captured.value)


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
    assert "parser_endpoint_url" in WorkerSettings.model_fields
    assert "parser_endpoint_url" not in ApiSettings.model_fields
    assert "parser_endpoint_url" not in ParserSettings.model_fields
    assert "query_providers" in ApiSettings.model_fields
    assert "query_providers" not in WorkerSettings.model_fields
    assert "query_providers" not in ParserSettings.model_fields
    assert "generation_provider" in ApiSettings.model_fields
    assert "generation_provider" not in WorkerSettings.model_fields
    assert "generation_provider" not in ParserSettings.model_fields
    assert "indexing_providers" in WorkerSettings.model_fields
    assert "indexing_providers" not in ApiSettings.model_fields
    assert "indexing_providers" not in ParserSettings.model_fields
    assert "provider_monthly_budget_micro_usd" in ApiSettings.model_fields
    assert "provider_monthly_budget_micro_usd" in WorkerSettings.model_fields
    assert "provider_monthly_budget_micro_usd" not in ParserSettings.model_fields


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
    monkeypatch.setenv("INVESTMENT_RESEARCH_MAX_UPLOAD_BYTES", "1048576")
    monkeypatch.setenv("INVESTMENT_RESEARCH_MAX_PDF_PAGES", "80")
    monkeypatch.setenv("INVESTMENT_RESEARCH_MAX_EXTRACTED_CHARACTERS", "500000")
    monkeypatch.setenv("INVESTMENT_RESEARCH_PARSER_CHILD_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("INVESTMENT_RESEARCH_PARSER_MAX_CONCURRENT_CHILDREN", "3")

    settings = load_parser_settings()

    assert settings.environment == "test"
    assert settings.runtime_location == "container"
    assert settings.log_level == "ERROR"
    assert settings.parser_host == "0.0.0.0"
    assert settings.parser_port == 8124
    assert settings.max_request_bytes == 1_048_576
    assert settings.max_pdf_pages == 80
    assert settings.max_extracted_characters == 500_000
    assert settings.child_timeout_seconds == 12
    assert settings.max_concurrent_children == 3


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("child_timeout_seconds", 0, id="zero-child-timeout"),
        pytest.param(
            "child_timeout_seconds",
            MAX_CONFIGURABLE_PARSER_CHILD_TIMEOUT_SECONDS + 1,
            id="excessive-child-timeout",
        ),
        pytest.param("max_concurrent_children", 0, id="zero-child-capacity"),
        pytest.param(
            "max_concurrent_children",
            MAX_CONFIGURABLE_PARSER_CONCURRENT_CHILDREN + 1,
            id="excessive-child-capacity",
        ),
    ],
)
def test_parser_settings_reject_unsafe_child_limits(field: str, value: int) -> None:
    with pytest.raises(ValidationError) as error:
        ParserSettings.model_validate({field: value})

    assert error.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "http://localhost:8100",
        "http://127.0.0.1:8100",
        "http://[::1]:8100",
    ],
)
def test_worker_parser_endpoint_allows_only_loopback_on_host(endpoint_url: str) -> None:
    settings = WorkerSettings(parser_endpoint_url=endpoint_url)

    assert settings.require_parser_endpoint_url() == endpoint_url


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "https://127.0.0.1:8100",
        "http://parser:8100",
        "http://example.invalid:8100",
        "http://user:password@127.0.0.1:8100",
        "http://127.0.0.1:8100/internal/v1/parse",
        "http://127.0.0.1:8100?target=other",
    ],
)
def test_worker_parser_endpoint_rejects_unsafe_host_target(endpoint_url: str) -> None:
    with pytest.raises(ValidationError, match="parser_endpoint_url"):
        WorkerSettings(parser_endpoint_url=endpoint_url)


def test_worker_parser_endpoint_allows_only_parser_service_in_container() -> None:
    settings = WorkerSettings(
        runtime_location="container",
        parser_endpoint_url="http://parser:8100",
    )

    assert settings.require_parser_endpoint_url() == "http://parser:8100"

    with pytest.raises(ValidationError, match="Compose parser service"):
        WorkerSettings(
            runtime_location="container",
            parser_endpoint_url="http://127.0.0.1:8100",
        )


def test_worker_requires_parser_endpoint_only_for_isolated_execution() -> None:
    with pytest.raises(RuntimeError, match="parser endpoint configuration"):
        WorkerSettings().require_parser_endpoint_url()


def test_worker_allows_m0_regression_profile_only_in_test_environment() -> None:
    settings = WorkerSettings(
        environment="test",
        processing_profile="m0-regression",
    )

    assert settings.processing_profile == "m0-regression"

    with pytest.raises(ValidationError, match="allowed only in the test environment"):
        WorkerSettings(processing_profile="m0-regression")


@pytest.mark.parametrize(
    "forbidden_name",
    [
        "INVESTMENT_RESEARCH_DATABASE_URL",
        "POSTGRES_PASSWORD",
        "MINIO_ROOT_PASSWORD",
        "INVESTMENT_RESEARCH_S3_SECRET_ACCESS_KEY",
        "INVESTMENT_RESEARCH_OPENAI_DOCUMENT_API_KEY",
        "INVESTMENT_RESEARCH_OPENAI_GENERATION_API_KEY",
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
