"""Unit tests for application resource wiring and lifecycle ownership."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from investment_research.bootstrap import ApplicationContainer, build_container
from investment_research.infrastructure.object_storage.local import LocalObjectStorage
from investment_research.main import create_app
from investment_research.settings import Settings

LOCAL_DATABASE_URL = "postgresql+psycopg://test-user:test-password@127.0.0.1:5432/test-database"


def make_settings(object_storage_root: Path) -> Settings:
    """Build deterministic local settings without opening a database connection."""

    return Settings.model_validate(
        {
            "environment": "test",
            "database_url": LOCAL_DATABASE_URL,
            "object_storage_root": object_storage_root,
        }
    )


def test_build_container_wires_application_resources(tmp_path: Path) -> None:
    storage_root = tmp_path / "objects"
    settings = make_settings(storage_root)
    container = build_container(settings)

    try:
        session = container.session_factory()
        try:
            assert container.settings is settings
            assert container.database_engine.dialect.name == "postgresql"
            assert session.bind is container.database_engine
            assert isinstance(container.object_storage, LocalObjectStorage)
            assert not storage_root.exists()
        finally:
            session.close()
    finally:
        container.close()


def test_application_lifespan_owns_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    closed_containers: list[ApplicationContainer] = []

    def record_close(container: ApplicationContainer) -> None:
        closed_containers.append(container)

    monkeypatch.setattr(ApplicationContainer, "close", record_close)
    application = create_app(make_settings(tmp_path / "objects"))

    assert not hasattr(application.state, "container")

    with TestClient(application):
        container = application.state.container
        assert isinstance(container, ApplicationContainer)

    assert closed_containers == [container]
    assert not hasattr(application.state, "container")
