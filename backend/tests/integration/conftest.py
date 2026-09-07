"""Shared fixtures for opt-in PostgreSQL integration tests."""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine

from investment_research.infrastructure.database.session import create_database_engine
from investment_research.settings import Settings, load_settings


@pytest.fixture(scope="session")
def integration_database_settings() -> Settings:
    """Load an explicitly configured test database or skip integration tests."""

    settings = load_settings()
    if settings.database_url is None:
        pytest.skip("INVESTMENT_RESEARCH_DATABASE_URL is not configured")
    if settings.environment != "test":
        pytest.fail("PostgreSQL integration tests require INVESTMENT_RESEARCH_ENVIRONMENT=test")
    return settings


@pytest.fixture(scope="session")
def integration_database_engine(
    integration_database_settings: Settings,
) -> Iterator[Engine]:
    """Provide one process-wide engine without creating or migrating schema."""

    engine = create_database_engine(integration_database_settings)
    try:
        if engine.dialect.name != "postgresql":
            pytest.fail("integration database must use PostgreSQL")
        yield engine
    finally:
        engine.dispose()
