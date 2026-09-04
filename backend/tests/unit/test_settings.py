"""Tests for local-only runtime settings."""

import pytest
from pydantic import ValidationError

from investment_research.settings import Settings, load_settings


def test_settings_load_supported_environment_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVESTMENT_RESEARCH_ENVIRONMENT", "test")
    monkeypatch.setenv("INVESTMENT_RESEARCH_API_HOST", "localhost")
    monkeypatch.setenv("INVESTMENT_RESEARCH_API_PORT", "8123")

    settings = load_settings()

    assert settings.environment == "test"
    assert settings.api_host == "localhost"
    assert settings.api_port == 8123


def test_settings_reject_non_loopback_api_host() -> None:
    with pytest.raises(ValidationError, match="loopback"):
        Settings(api_host="0.0.0.0")
