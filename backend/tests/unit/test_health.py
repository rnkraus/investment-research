"""Tests for the public health endpoint."""

from fastapi.testclient import TestClient

from investment_research.main import create_app
from investment_research.settings import Settings


def test_health_reports_process_liveness_without_private_details() -> None:
    client = TestClient(create_app(Settings(environment="test")))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "Investment Research Archive",
        "version": "0.0.0",
    }
