"""Tests for the public health endpoint."""

from fastapi.testclient import TestClient

from investment_research import __version__
from investment_research.main import create_app
from investment_research.settings import ApiSettings

LOCAL_DATABASE_URL = "postgresql+psycopg://test-user:test-password@127.0.0.1:5432/test-database"


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
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "Injected Test Research Archive",
        "version": __version__,
    }
