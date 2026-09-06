"""Tests for local-only runtime settings."""

import pytest
from pydantic import ValidationError

from investment_research.settings import Settings, load_settings

LOCAL_DATABASE_URL = "postgresql+psycopg://test-user:test-password@127.0.0.1:5432/test-database"


def test_settings_load_supported_environment_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVESTMENT_RESEARCH_ENVIRONMENT", "test")
    monkeypatch.setenv("INVESTMENT_RESEARCH_API_HOST", "localhost")
    monkeypatch.setenv("INVESTMENT_RESEARCH_API_PORT", "8123")
    monkeypatch.setenv("INVESTMENT_RESEARCH_DATABASE_URL", LOCAL_DATABASE_URL)

    settings = load_settings()

    assert settings.environment == "test"
    assert settings.api_host == "localhost"
    assert settings.api_port == 8123
    assert settings.require_database_url() == LOCAL_DATABASE_URL
    assert "test-password" not in repr(settings)


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
    ],
)
def test_settings_reject_unsafe_database_url(
    database_url: str,
    expected_error: str,
) -> None:
    with pytest.raises(ValidationError, match=expected_error):
        Settings.model_validate({"database_url": database_url})


def test_settings_require_database_url_for_database_access() -> None:
    with pytest.raises(RuntimeError, match="INVESTMENT_RESEARCH_DATABASE_URL"):
        Settings().require_database_url()
