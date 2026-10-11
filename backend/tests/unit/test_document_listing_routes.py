"""HTTP checks for bounded document metadata and batched processing status."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, create_autospec
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from investment_research.api.dependencies import (
    get_list_documents_service,
    get_processing_run_service,
    get_workspace_context,
)
from investment_research.main import create_app
from investment_research.modules.documents.contracts import (
    DocumentListCursor,
    DocumentListEntry,
    DocumentListPage,
    DocumentListQuery,
)
from investment_research.modules.documents.domain import OriginalMediaType
from investment_research.modules.documents.listing import ListDocumentsService
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.application import (
    GetProcessingRunService,
    create_captured_processing_run,
)
from investment_research.modules.processing.contracts import ProcessingStatusQuery
from investment_research.settings import ApiSettings

CREATED_AT = datetime(2026, 10, 11, 12, tzinfo=UTC)
CONTEXT = WorkspaceContext(user_id=UUID(int=1), workspace_id=UUID(int=2))


def make_entry() -> DocumentListEntry:
    """Return one public-synthetic current-original metadata snapshot."""

    return DocumentListEntry(
        document_id=UUID(int=3),
        document_version_id=UUID(int=4),
        title="Synthetic catalog document",
        language="en",
        media_type=OriginalMediaType.PDF,
        byte_size=128,
        created_at=CREATED_AT,
        captured_at=CREATED_AT,
    )


@pytest.fixture
def catalog_client(tmp_path: Path) -> Iterator[tuple[TestClient, MagicMock, MagicMock]]:
    """Exercise the real route without storage, database, queue, or paid-provider calls."""

    application = create_app(
        ApiSettings.model_validate(
            {
                "environment": "test",
                "database_url": "postgresql+psycopg://test-user:test-password@127.0.0.1:1/test",
                "redis_url": "redis://:synthetic-secret@127.0.0.1:1/0",
                "object_storage_root": tmp_path / "objects",
            }
        )
    )
    documents = create_autospec(ListDocumentsService, instance=True)
    processing = create_autospec(GetProcessingRunService, instance=True)
    documents.list_documents.return_value = DocumentListPage(items=(make_entry(),))
    processing.get_latest_for_versions.return_value = ()
    application.dependency_overrides[get_list_documents_service] = lambda: cast(
        ListDocumentsService, documents
    )
    application.dependency_overrides[get_processing_run_service] = lambda: cast(
        GetProcessingRunService, processing
    )
    application.dependency_overrides[get_workspace_context] = lambda: CONTEXT

    with TestClient(application) as client:
        yield client, documents, processing


def test_document_catalog_composes_safe_status_and_current_original_link(
    catalog_client: tuple[TestClient, MagicMock, MagicMock],
) -> None:
    client, documents, processing = catalog_client
    entry = make_entry()
    run = create_captured_processing_run(
        context=CONTEXT,
        document_version_id=entry.document_version_id,
        processing_run_id=UUID(int=5),
        created_at=CREATED_AT,
    )
    processing.get_latest_for_versions.return_value = (run,)

    response = client.get("/api/v1/documents")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {
        "items": [
            {
                **entry.model_dump(mode="json"),
                "processing": {
                    "processing_run_id": str(run.id),
                    "document_version_id": str(entry.document_version_id),
                    "state": "CAPTURED",
                    "error_summary": None,
                },
                "original_url": (
                    f"/api/v1/documents/{entry.document_id}"
                    f"/versions/{entry.document_version_id}/original"
                ),
            }
        ],
        "next_cursor": None,
    }
    documents.list_documents.assert_called_once_with(CONTEXT, DocumentListQuery())
    processing.get_latest_for_versions.assert_called_once_with(
        CONTEXT, ProcessingStatusQuery(document_version_ids=(entry.document_version_id,))
    )
    for private_field in ("workspace_id", "storage_key", "checksum_sha256", "parser_version"):
        assert private_field not in response.text


def test_document_catalog_preserves_missing_status_and_maps_cursor_to_utc(
    catalog_client: tuple[TestClient, MagicMock, MagicMock],
) -> None:
    client, documents, _ = catalog_client
    entry = make_entry()
    cursor = DocumentListCursor(created_at=CREATED_AT, document_id=entry.document_id)
    documents.list_documents.return_value = DocumentListPage(items=(entry,), next_cursor=cursor)

    response = client.get(
        "/api/v1/documents",
        params={
            "page_size": "1",
            "after_created_at": "2026-10-11T14:00:00+02:00",
            "after_document_id": str(entry.document_id),
        },
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["processing"] is None
    assert response.json()["next_cursor"] == cursor.model_dump(mode="json")
    documents.list_documents.assert_called_once_with(
        CONTEXT, DocumentListQuery(page_size=1, after=cursor)
    )


@pytest.mark.parametrize(
    "query_string",
    [
        "?page_size=0",
        "?page_size=101",
        "?page_size=invalid",
        "?after_created_at=2026-10-11T12:00:00Z",
        f"?after_document_id={UUID(int=3)}",
        f"?after_created_at=2026-10-11T12:00:00&after_document_id={UUID(int=3)}",
        "?after_created_at=invalid&after_document_id=invalid",
        f"?workspace_id={UUID(int=99)}",
        "?unexpected=private-value",
    ],
)
def test_document_catalog_rejects_invalid_or_client_scoped_queries_before_services(
    catalog_client: tuple[TestClient, MagicMock, MagicMock], query_string: str
) -> None:
    client, documents, processing = catalog_client

    response = client.get(f"/api/v1/documents{query_string}")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert "private-value" not in response.text
    documents.list_documents.assert_not_called()
    processing.get_latest_for_versions.assert_not_called()


@pytest.mark.parametrize("fails_in_processing", [False, True])
def test_document_catalog_hides_database_details_without_partial_success(
    catalog_client: tuple[TestClient, MagicMock, MagicMock], fails_in_processing: bool
) -> None:
    client, documents, processing = catalog_client
    failing_method = (
        processing.get_latest_for_versions if fails_in_processing else documents.list_documents
    )
    failing_method.side_effect = SQLAlchemyError("private database detail")

    response = client.get("/api/v1/documents")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["error"]["message"] == "document list could not be read"
    assert "private database detail" not in response.text
    assert "items" not in response.json()


def test_document_catalog_empty_page_returns_empty_items(
    catalog_client: tuple[TestClient, MagicMock, MagicMock],
) -> None:
    client, documents, processing = catalog_client
    documents.list_documents.return_value = DocumentListPage(items=())

    response = client.get("/api/v1/documents")

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}
    processing.get_latest_for_versions.assert_called_once_with(
        CONTEXT, ProcessingStatusQuery(document_version_ids=())
    )
