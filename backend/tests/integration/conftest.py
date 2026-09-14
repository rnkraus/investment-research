"""Shared fixtures for opt-in PostgreSQL integration tests."""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine

from investment_research.infrastructure.database.session import create_database_engine
from investment_research.settings import (
    ApiSettings,
    DatabaseSettings,
    WorkerSettings,
    load_database_settings,
)


@pytest.fixture(scope="session")
def integration_database_settings() -> DatabaseSettings:
    """Load an explicitly configured test database or skip integration tests."""

    settings = load_database_settings()
    if settings.database_url is None:
        pytest.skip("INVESTMENT_RESEARCH_DATABASE_URL is not configured")
    if settings.environment != "test":
        pytest.fail("PostgreSQL integration tests require INVESTMENT_RESEARCH_ENVIRONMENT=test")
    return settings


@pytest.fixture(scope="session")
def integration_api_settings(
    integration_database_settings: DatabaseSettings,
) -> ApiSettings:
    """Provide API settings bound to the configured integration database."""

    return ApiSettings.model_validate(integration_database_settings.model_dump())


@pytest.fixture(scope="session")
def integration_worker_settings(
    integration_database_settings: DatabaseSettings,
) -> WorkerSettings:
    """Provide worker settings bound to the configured integration database."""

    return WorkerSettings.model_validate(integration_database_settings.model_dump())


@pytest.fixture(scope="session")
def integration_database_engine(
    integration_database_settings: DatabaseSettings,
) -> Iterator[Engine]:
    """Provide one process-wide engine without creating or migrating schema."""

    engine = create_database_engine(integration_database_settings)
    try:
        if engine.dialect.name != "postgresql":
            pytest.fail("integration database must use PostgreSQL")
        yield engine
    finally:
        engine.dispose()
