"""PostgreSQL API tests for processing-status workspace isolation."""

from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from investment_research.api.dependencies import get_workspace_context
from investment_research.main import create_app
from investment_research.settings import ApiSettings
from tests.integration.factories import (
    make_research_source,
    persist_research_source,
    persist_workspace,
)


def test_processing_status_hides_cross_workspace_and_unknown_runs(
    integration_api_settings: ApiSettings,
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    owner = make_research_source(label="Status Owner")
    foreign = make_research_source(label="Status Foreign")
    persist_research_source(integration_database_engine, owner)
    persist_workspace(integration_database_engine, foreign)
    application = create_app(
        integration_api_settings.model_copy(update={"object_storage_root": tmp_path / "objects"})
    )
    application.dependency_overrides[get_workspace_context] = lambda: owner.context

    with TestClient(application) as client:
        visible_response = client.get(f"/api/v1/processing-runs/{owner.processing_run.id}")
        unknown_response = client.get(f"/api/v1/processing-runs/{uuid4()}")

        application.dependency_overrides[get_workspace_context] = lambda: foreign.context
        foreign_response = client.get(f"/api/v1/processing-runs/{owner.processing_run.id}")

    assert visible_response.status_code == 200
    assert visible_response.json() == {
        "processing_run_id": str(owner.processing_run.id),
        "document_version_id": str(owner.version.id),
        "state": "LEXICAL_READY",
        "error_summary": None,
    }
    assert visible_response.headers["cache-control"] == "private, no-store"
    assert unknown_response.status_code == 404
    assert foreign_response.status_code == 404
    unknown_error = unknown_response.json()["error"]
    foreign_error = foreign_response.json()["error"]
    assert unknown_error["code"] == foreign_error["code"] == "NOT_FOUND"
    assert unknown_error["message"] == foreign_error["message"]
    assert UUID(unknown_error["request_id"])
    assert UUID(foreign_error["request_id"])
    assert unknown_response.headers["x-request-id"] == unknown_error["request_id"]
    assert foreign_response.headers["x-request-id"] == foreign_error["request_id"]
    assert unknown_error["request_id"] != foreign_error["request_id"]
