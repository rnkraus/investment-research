"""PostgreSQL integration test for layered API health reporting."""

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from investment_research.infrastructure.health import PostgreSQLReadinessProbe
from investment_research.main import create_app
from investment_research.settings import ApiSettings


def test_postgresql_readiness_accepts_the_migrated_database(
    integration_database_engine: Engine,
) -> None:
    assert PostgreSQLReadinessProbe(integration_database_engine).is_ready()


def test_api_is_live_but_not_ready_when_the_required_queue_is_unavailable(
    integration_api_settings: ApiSettings,
    tmp_path: Path,
) -> None:
    application = create_app(
        integration_api_settings.model_copy(update={"object_storage_root": tmp_path / "objects"})
    )

    with TestClient(application) as client:
        liveness_response = client.get("/health")
        readiness_response = client.get("/ready")

    assert liveness_response.status_code == 200
    assert liveness_response.json()["status"] == "ok"
    assert readiness_response.status_code == 503
    assert readiness_response.json() == {"status": "not_ready"}
    assert readiness_response.headers["cache-control"] == "no-store"
