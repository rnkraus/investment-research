"""Tests for the public health endpoint."""

from dataclasses import dataclass, replace

from fastapi.testclient import TestClient

from investment_research import __version__
from investment_research.main import create_app
from investment_research.settings import ApiSettings

LOCAL_DATABASE_URL = "postgresql+psycopg://test-user:test-password@127.0.0.1:5432/test-database"


@dataclass(slots=True)
class StubReadinessProbe:
    """Return one configured result and record whether readiness was requested."""

    result: bool
    calls: int = 0

    def is_ready(self) -> bool:
        self.calls += 1
        return self.result


def test_health_reports_process_liveness_without_private_details() -> None:
    application = create_app(
        ApiSettings.model_validate(
            {
                "app_name": "Injected Test Research Archive",
                "environment": "test",
                "database_url": LOCAL_DATABASE_URL,
            }
        )
    )

    with TestClient(application) as client:
        readiness_probe = StubReadinessProbe(result=False)
        application.state.container = replace(
            application.state.container,
            readiness_probe=readiness_probe,
        )
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "Injected Test Research Archive",
        "version": __version__,
    }
    assert response.headers["cache-control"] == "no-store"
    assert readiness_probe.calls == 0


def test_ready_reports_only_the_aggregate_dependency_state() -> None:
    application = create_app(
        ApiSettings.model_validate(
            {
                "environment": "test",
                "database_url": LOCAL_DATABASE_URL,
            }
        )
    )

    with TestClient(application) as client:
        readiness_probe = StubReadinessProbe(result=True)
        application.state.container = replace(
            application.state.container,
            readiness_probe=readiness_probe,
        )
        ready_response = client.get("/ready")

        readiness_probe.result = False
        unavailable_response = client.get("/ready")

    assert ready_response.status_code == 200
    assert ready_response.json() == {"status": "ready"}
    assert ready_response.headers["cache-control"] == "no-store"
    assert unavailable_response.status_code == 503
    assert unavailable_response.json() == {"status": "not_ready"}
    assert unavailable_response.headers["cache-control"] == "no-store"
    assert readiness_probe.calls == 2
    assert "database" not in unavailable_response.text
    assert "postgresql" not in unavailable_response.text
