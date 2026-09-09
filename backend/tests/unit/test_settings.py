"""Tests for local-only runtime settings."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from investment_research.settings import PROJECT_ROOT, Settings, load_settings

LOCAL_DATABASE_URL = "postgresql+psycopg://test-user:test-password@127.0.0.1:5432/test-database"
POSTGRES_SETTING_NAMES = (
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_PORT",
)


def test_settings_load_supported_environment_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    object_storage_root = tmp_path / "objects"
    monkeypatch.setenv("INVESTMENT_RESEARCH_ENVIRONMENT", "test")
    monkeypatch.setenv("INVESTMENT_RESEARCH_API_HOST", "localhost")
    monkeypatch.setenv("INVESTMENT_RESEARCH_API_PORT", "8123")
    monkeypatch.setenv("INVESTMENT_RESEARCH_DATABASE_URL", LOCAL_DATABASE_URL)
    monkeypatch.setenv(
        "INVESTMENT_RESEARCH_OBJECT_STORAGE_ROOT",
        str(object_storage_root),
    )

    settings = load_settings(env_file=None)

    assert settings.environment == "test"
    assert settings.api_host == "localhost"
    assert settings.api_port == 8123
    assert settings.require_database_url() == LOCAL_DATABASE_URL
    assert settings.object_storage_root == object_storage_root
    assert "test-password" not in repr(settings)


def test_settings_resolve_relative_object_storage_root_from_project_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "INVESTMENT_RESEARCH_OBJECT_STORAGE_ROOT",
        "output/test-object-storage",
    )

    settings = load_settings(env_file=None)

    assert settings.object_storage_root == PROJECT_ROOT / "output" / "test-object-storage"


def test_settings_reject_object_storage_inside_frontend() -> None:
    with pytest.raises(ValidationError, match="outside the frontend web root"):
        Settings(object_storage_root=PROJECT_ROOT / "frontend" / "uploads")


def test_settings_build_database_url_from_local_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in (
        "INVESTMENT_RESEARCH_DATABASE_URL",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_PORT",
    ):
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

    settings = load_settings(env_file=env_file)

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

    settings = load_settings(env_file=env_file)

    assert settings.require_database_url() == LOCAL_DATABASE_URL


@pytest.mark.parametrize(
    "missing_name",
    ["POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_PORT"],
)
def test_settings_reject_incomplete_local_postgres_configuration(
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
        load_settings(env_file=env_file)


@pytest.mark.parametrize("postgres_port", ["not-a-number", "0", "65536"])
def test_settings_reject_invalid_local_postgres_port(
    postgres_port: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in ("INVESTMENT_RESEARCH_DATABASE_URL", "POSTGRES_PASSWORD", "POSTGRES_PORT"):
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
        load_settings(env_file=env_file)


def test_settings_reject_non_loopback_api_host() -> None:
    with pytest.raises(ValidationError, match="loopback"):
        Settings(api_host="0.0.0.0")


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
        pytest.param(
            "postgresql+psycopg://test-user:test-password@127.0.0.1/test-database"
            "?hostaddr=203.0.113.10",
            "query parameters",
            id="query-hostaddr-override",
        ),
        pytest.param(
            "postgresql+psycopg://test-user:test-password@127.0.0.1/test-database"
            "?service=external-service",
            "query parameters",
            id="query-service-override",
        ),
        pytest.param(
            "postgresql+psycopg://test-user:test-password@127.0.0.1/test-database"
            "?application_name=research-api",
            "query parameters",
            id="all-query-options-rejected",
        ),
    ],
)
def test_settings_reject_unsafe_database_url(
    database_url: str,
    expected_error: str,
) -> None:
    with pytest.raises(ValidationError, match=expected_error):
        Settings.model_validate({"database_url": database_url})


def test_settings_require_database_url_for_database_access() -> None:
    with pytest.raises(RuntimeError, match="PostgreSQL configuration"):
        Settings().require_database_url()
