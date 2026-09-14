"""End-to-end document API tests against PostgreSQL and local object storage."""

from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from investment_research.api.contracts import DocumentAccepted
from investment_research.api.dependencies import get_workspace_context
from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.infrastructure.object_storage.local import LocalObjectStorage
from investment_research.main import create_app
from investment_research.modules.documents.persistence import SqlAlchemyDocumentRepository
from investment_research.modules.identity.application import (
    LOCAL_DEVELOPMENT_USER_ID,
    LOCAL_DEVELOPMENT_WORKSPACE_ID,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.processing.persistence import SqlAlchemyProcessingRepository
from investment_research.settings import ApiSettings
from scripts.seed_m0_0 import ISOLATION_TEST_WORKSPACE_ID

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_PDF = (
    REPOSITORY_ROOT / "fixtures/m0_0/documents/northstar-grid-analytics-fy2025-earnings-brief.pdf"
)


def capture_fixture(client: TestClient, *, title: str) -> DocumentAccepted:
    """Upload the synthetic PDF through the real multipart boundary."""

    with FIXTURE_PDF.open("rb") as fixture_file:
        response = client.post(
            "/api/v1/documents",
            data={
                "title": title,
                "language": "en",
                "published_at": "2026-02-19T00:00:00Z",
                "available_at": "2026-02-19T00:00:00Z",
            },
            files={"file": (FIXTURE_PDF.name, fixture_file, "application/pdf")},
        )

    assert response.status_code == 202
    return DocumentAccepted.model_validate(response.json())


def test_upload_and_open_original_enforce_canonical_workspace(
    integration_api_settings: ApiSettings,
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "objects"
    settings = integration_api_settings.model_copy(
        update={
            "object_storage_root": storage_root,
            "max_upload_bytes": FIXTURE_PDF.stat().st_size,
        }
    )
    application = create_app(settings)

    with TestClient(application) as client:
        payload = capture_fixture(
            client,
            title="Northstar Grid Analytics Ltd. - FY2025 Earnings Brief",
        )
        second_payload = capture_fixture(client, title="Second synthetic version owner")
        document_id = payload.document_id
        version_id = payload.document_version_id
        processing_run_id = payload.processing_run_id

        original_response = client.get(
            f"/api/v1/documents/{document_id}/versions/{version_id}/original"
        )
        mismatched_response = client.get(
            f"/api/v1/documents/{document_id}/versions/"
            f"{second_payload.document_version_id}/original"
        )

        foreign_context = WorkspaceContext(
            user_id=LOCAL_DEVELOPMENT_USER_ID,
            workspace_id=ISOLATION_TEST_WORKSPACE_ID,
        )
        application.dependency_overrides[get_workspace_context] = lambda: foreign_context
        foreign_response = client.get(
            f"/api/v1/documents/{document_id}/versions/{version_id}/original"
        )

    assert payload.state == "CAPTURED"
    assert payload.processing_status_url == (f"/api/v1/processing-runs/{processing_run_id}")
    assert original_response.status_code == 200
    assert original_response.content == FIXTURE_PDF.read_bytes()
    assert original_response.headers["content-type"] == "application/pdf"
    assert original_response.headers["x-content-type-options"] == "nosniff"
    assert mismatched_response.status_code == 404
    assert foreign_response.status_code == 404
    mismatched_error = mismatched_response.json()["error"]
    foreign_error = foreign_response.json()["error"]
    assert mismatched_error["code"] == foreign_error["code"] == "NOT_FOUND"
    assert mismatched_error["message"] == foreign_error["message"]
    assert UUID(mismatched_error["request_id"])
    assert UUID(foreign_error["request_id"])
    assert mismatched_response.headers["x-request-id"] == mismatched_error["request_id"]
    assert foreign_response.headers["x-request-id"] == foreign_error["request_id"]
    assert mismatched_error["request_id"] != foreign_error["request_id"]

    context = WorkspaceContext(
        user_id=LOCAL_DEVELOPMENT_USER_ID,
        workspace_id=LOCAL_DEVELOPMENT_WORKSPACE_ID,
    )
    session_factory = create_session_factory(integration_database_engine)
    with transactional_session(session_factory) as session:
        documents = SqlAlchemyDocumentRepository(session)
        processing = SqlAlchemyProcessingRepository(session)
        document = documents.get_document_by_id(context, document_id)
        version = documents.get_current_version_by_id(context, document_id, version_id)
        original = documents.get_original_artifact(context, document_id, version_id)
        processing_run = processing.get_run_by_id(context, processing_run_id)

    assert document is not None
    assert version is not None
    assert original is not None
    assert processing_run is not None
    assert document.current_version_id == version.id
    assert version.content_sha256 == original.checksum_sha256
    assert original.byte_size == FIXTURE_PDF.stat().st_size
    assert processing_run.document_version_id == version.id
    assert processing_run.state is ProcessingState.CAPTURED

    storage = LocalObjectStorage(storage_root)
    with storage.open(context, original.storage_key) as stored_file:
        assert stored_file.read() == FIXTURE_PDF.read_bytes()
