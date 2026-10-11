"""Safe HTTP boundaries for explicitly confirmed irreversible batch deletion."""

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
    get_document_deletion_service,
    get_submit_document_deletion_service,
    get_workspace_context,
)
from investment_research.main import create_app
from investment_research.modules.documents.deletion import (
    GetDocumentDeletionService,
    SubmitDocumentDeletionService,
)
from investment_research.modules.documents.deletion_contracts import (
    DeleteDocumentsCommand,
    DocumentDeletionReceipt,
    DocumentDeletionState,
    DocumentDeletionSubmission,
)
from investment_research.modules.documents.deletion_ports import (
    DocumentDeletionBusyError,
    DocumentDeletionUnavailableError,
    DocumentPurgePendingError,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.settings import ApiSettings

CONTEXT = WorkspaceContext(user_id=UUID(int=1), workspace_id=UUID(int=2))
DOCUMENT_ID = UUID(int=3)
NOW = datetime(2026, 10, 12, tzinfo=UTC)
STATUS_PATH = f"/api/v1/documents/{DOCUMENT_ID}/deletion"


@pytest.fixture
def deletion_client(tmp_path: Path) -> Iterator[tuple[TestClient, MagicMock, MagicMock]]:
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
    submit = create_autospec(SubmitDocumentDeletionService, instance=True)
    reads = create_autospec(GetDocumentDeletionService, instance=True)
    receipt = DocumentDeletionReceipt(
        workspace_id=CONTEXT.workspace_id,
        document_id=DOCUMENT_ID,
        state=DocumentDeletionState.PENDING,
        requested_at=NOW,
    )
    submit.submit.return_value = DocumentDeletionSubmission((receipt,), ())
    reads.get.return_value = receipt
    application.dependency_overrides[get_submit_document_deletion_service] = lambda: cast(
        SubmitDocumentDeletionService, submit
    )
    application.dependency_overrides[get_document_deletion_service] = lambda: cast(
        GetDocumentDeletionService, reads
    )
    application.dependency_overrides[get_workspace_context] = lambda: CONTEXT
    with TestClient(application) as client:
        yield client, submit, reads


def test_confirmed_deletion_returns_only_content_free_status_and_failed_publications(
    deletion_client: tuple[TestClient, MagicMock, MagicMock],
) -> None:
    client, submit, _ = deletion_client
    original = submit.submit.return_value
    submit.submit.return_value = DocumentDeletionSubmission(original.receipts, (DOCUMENT_ID,))
    response = client.post(
        "/api/v1/documents/deletions",
        json={
            "document_ids": [str(DOCUMENT_ID)],
            "confirm_permanent": True,
        },
    )
    assert response.status_code == 202
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {
        "items": [
            {
                "document_id": str(DOCUMENT_ID),
                "state": "PENDING",
                "requested_at": "2026-10-12T00:00:00Z",
                "completed_at": None,
                "status_url": STATUS_PATH,
            }
        ],
        "dispatch_pending_document_ids": [str(DOCUMENT_ID)],
    }
    submit.submit.assert_called_once_with(
        CONTEXT, DeleteDocumentsCommand(document_ids=(DOCUMENT_ID,))
    )
    for field in ("workspace_id", "storage_key", "title", "vector_ids", "output_text"):
        assert field not in response.text


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"document_ids": [], "confirm_permanent": True},
        {"document_ids": [str(DOCUMENT_ID)]},
        *(
            {"document_ids": [str(DOCUMENT_ID)], "confirm_permanent": value}
            for value in (False, 1, "true", None)
        ),
        {"document_ids": ["private-invalid-id"], "confirm_permanent": True},
        {"document_ids": [str(DOCUMENT_ID)] * 2, "confirm_permanent": True},
        {"document_ids": [str(UUID(int=n)) for n in range(101)], "confirm_permanent": True},
        {
            "document_ids": [str(DOCUMENT_ID)],
            "confirm_permanent": True,
            "workspace_id": str(CONTEXT.workspace_id),
        },
        {
            "document_ids": [str(DOCUMENT_ID)],
            "confirm_permanent": True,
            "storage_key": "private-path",
        },
    ],
)
def test_invalid_or_unconfirmed_batches_never_call_the_service(
    deletion_client: tuple[TestClient, MagicMock, MagicMock],
    payload: object,
) -> None:
    client, submit, _ = deletion_client
    response = client.post("/api/v1/documents/deletions", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert "private-" not in response.text
    submit.submit.assert_not_called()


@pytest.mark.parametrize(
    "failure,expected_status,code",
    [
        (DocumentDeletionUnavailableError("private target"), 404, "NOT_FOUND"),
        (DocumentDeletionBusyError("private state"), 409, "CONFLICT"),
        (DocumentPurgePendingError("private state"), 500, "INTERNAL_ERROR"),
        (SQLAlchemyError("private database"), 500, "INTERNAL_ERROR"),
    ],
)
def test_admission_errors_are_safe_and_never_claim_acceptance(
    deletion_client: tuple[TestClient, MagicMock, MagicMock],
    failure: Exception,
    expected_status: int,
    code: str,
) -> None:
    client, submit, _ = deletion_client
    submit.submit.side_effect = failure
    response = client.post(
        "/api/v1/documents/deletions",
        json={
            "document_ids": [str(DOCUMENT_ID)],
            "confirm_permanent": True,
        },
    )
    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == code
    assert "private" not in response.text
    assert "items" not in response.json()
    assert UUID(response.json()["error"]["request_id"])


@pytest.mark.parametrize("completed", [False, True])
def test_status_is_scoped_content_free_and_uncached(
    deletion_client: tuple[TestClient, MagicMock, MagicMock],
    completed: bool,
) -> None:
    client, _, reads = deletion_client
    if completed:
        reads.get.return_value = reads.get.return_value.model_copy(
            update={
                "state": DocumentDeletionState.COMPLETED,
                "completed_at": NOW,
            }
        )
    response = client.get(STATUS_PATH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["state"] == ("COMPLETED" if completed else "PENDING")
    assert response.json()["status_url"] == STATUS_PATH
    assert set(response.json()) == {
        "document_id",
        "state",
        "requested_at",
        "completed_at",
        "status_url",
    }
    reads.get.assert_called_once_with(CONTEXT, DOCUMENT_ID)


@pytest.mark.parametrize(
    "failure,expected_status",
    [
        (DocumentDeletionUnavailableError("private target"), 404),
        (DocumentPurgePendingError("private database"), 500),
        (SQLAlchemyError("private database"), 500),
    ],
)
def test_status_failures_do_not_disclose_internal_details(
    deletion_client: tuple[TestClient, MagicMock, MagicMock],
    failure: Exception,
    expected_status: int,
) -> None:
    client, _, reads = deletion_client
    reads.get.side_effect = failure
    response = client.get(STATUS_PATH)
    assert response.status_code == expected_status
    assert "private" not in response.text


def test_invalid_status_id_and_unsupported_delete_methods_do_not_execute(
    deletion_client: tuple[TestClient, MagicMock, MagicMock],
) -> None:
    client, submit, reads = deletion_client
    assert client.get("/api/v1/documents/invalid/deletion").status_code == 422
    assert client.delete("/api/v1/documents").status_code == 405
    submit.submit.assert_not_called()
    reads.get.assert_not_called()
