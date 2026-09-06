"""Unit tests for application resource wiring and lifecycle ownership."""

import pytest
from fastapi.testclient import TestClient

from investment_research.bootstrap import ApplicationContainer, build_container
from investment_research.main import create_app
from investment_research.settings import Settings

LOCAL_DATABASE_URL = "postgresql+psycopg://test-user:test-password@127.0.0.1:5432/test-database"


def make_settings() -> Settings:
    """Build deterministic local settings without opening a database connection."""

    return Settings.model_validate({"environment": "test", "database_url": LOCAL_DATABASE_URL})


def test_build_container_wires_database_resources() -> None:
    settings = make_settings()
    container = build_container(settings)

    try:
        session = container.session_factory()
        try:
            assert container.settings is settings
            assert container.database_engine.dialect.name == "postgresql"
            assert session.bind is container.database_engine
        finally:
            session.close()
    finally:
        container.close()


def test_application_lifespan_owns_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed_containers: list[ApplicationContainer] = []

    def record_close(container: ApplicationContainer) -> None:
        closed_containers.append(container)

    monkeypatch.setattr(ApplicationContainer, "close", record_close)
    application = create_app(make_settings())

    assert not hasattr(application.state, "container")

    with TestClient(application):
        container = application.state.container
        assert isinstance(container, ApplicationContainer)

    assert closed_containers == [container]
    assert not hasattr(application.state, "container")
