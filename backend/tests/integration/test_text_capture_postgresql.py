"""Pasted-text capture through the real API, PostgreSQL, and local object storage."""

from hashlib import sha256
from pathlib import Path

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
from investment_research.modules.documents.domain import OriginalMediaType
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
FIXTURE_TEXT = (
    REPOSITORY_ROOT / "fixtures/m1/documents/northstar-grid-analytics-fy2026-capacity-update.txt"
)
EXPECTED_SHA256 = "45985d7e98d74a7690513624914b49c6d9180842097f837798ce3f42803639b5"


def test_capture_and_open_text_original_enforce_canonical_workspace(
    integration_api_settings: ApiSettings,
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    """Preserve canonical UTF-8 bytes and hide them from a foreign workspace."""

    original_text = FIXTURE_TEXT.read_text(encoding="utf-8")
    original_bytes = original_text.encode("utf-8")
    assert sha256(original_bytes).hexdigest() == EXPECTED_SHA256
    storage_root = tmp_path / "objects"
    settings = integration_api_settings.model_copy(
        update={
            "object_storage_root": storage_root,
            "max_upload_bytes": len(original_bytes),
        }
    )
    application = create_app(settings)

    with TestClient(application) as client:
        capture_response = client.post(
            "/api/v1/documents/text",
            json={
                "title": "Northstar Grid Analytics Ltd. - FY2026 Capacity Update",
                "language": "en",
                "published_at": "2026-07-22T00:00:00Z",
                "available_at": "2026-07-22T00:00:00Z",
                "text": original_text,
            },
        )
        assert capture_response.status_code == 202
        capture = DocumentAccepted.model_validate(capture_response.json())
        original_response = client.get(
            f"/api/v1/documents/{capture.document_id}/versions/"
            f"{capture.document_version_id}/original"
        )

        foreign_context = WorkspaceContext(
            user_id=LOCAL_DEVELOPMENT_USER_ID,
            workspace_id=ISOLATION_TEST_WORKSPACE_ID,
        )
        application.dependency_overrides[get_workspace_context] = lambda: foreign_context
        foreign_response = client.get(
            f"/api/v1/documents/{capture.document_id}/versions/"
            f"{capture.document_version_id}/original"
        )

    assert capture.state == "CAPTURED"
    assert original_response.status_code == 200
    assert original_response.content == original_bytes
    assert original_response.headers["content-type"] == "text/plain; charset=utf-8"
    assert original_response.headers["cache-control"] == "private, no-store"
    assert original_response.headers["x-content-type-options"] == "nosniff"
    assert original_response.headers["content-disposition"].endswith('.txt"')
    assert foreign_response.status_code == 404
    assert foreign_response.json()["error"]["code"] == "NOT_FOUND"

    context = WorkspaceContext(
        user_id=LOCAL_DEVELOPMENT_USER_ID,
        workspace_id=LOCAL_DEVELOPMENT_WORKSPACE_ID,
    )
    session_factory = create_session_factory(integration_database_engine)
    with transactional_session(session_factory) as session:
        documents = SqlAlchemyDocumentRepository(session)
        processing = SqlAlchemyProcessingRepository(session)
        version = documents.get_current_version_by_id(
            context,
            capture.document_id,
            capture.document_version_id,
        )
        original = documents.get_original_artifact(
            context,
            capture.document_id,
            capture.document_version_id,
        )
        processing_run = processing.get_run_by_id(context, capture.processing_run_id)

    assert version is not None
    assert original is not None
    assert processing_run is not None
    assert original.media_type is OriginalMediaType.PLAIN_TEXT
    assert original.storage_key.endswith(".txt")
    assert original.byte_size == len(original_bytes)
    assert original.checksum_sha256 == version.content_sha256 == EXPECTED_SHA256
    assert processing_run.document_version_id == version.id
    assert processing_run.state is ProcessingState.CAPTURED

    with LocalObjectStorage(storage_root).open(context, original.storage_key) as stored_file:
        assert stored_file.read() == original_bytes
