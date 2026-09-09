"""Unit tests for the document-capture HTTP boundary."""

from collections.abc import Iterator
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from investment_research.api.dependencies import (
    get_capture_document_service,
    get_workspace_context,
)
from investment_research.api.middleware import MULTIPART_OVERHEAD_ALLOWANCE_BYTES
from investment_research.main import create_app
from investment_research.modules.documents.application import (
    CaptureDocumentService,
    InvalidPdfSignatureError,
    UnsupportedDocumentTypeError,
    UploadTooLargeError,
)
from investment_research.modules.documents.contracts import (
    CaptureDocumentInput,
    CaptureDocumentResult,
)
from investment_research.modules.documents.ports import (
    BinaryReadable,
    ObjectAlreadyExistsError,
    ObjectStorageError,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.domain import ProcessingState
from investment_research.settings import Settings

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
OTHER_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000099")
DOCUMENT_ID = UUID("10000000-0000-4000-8000-000000000001")
VERSION_ID = UUID("10000000-0000-4000-8000-000000000002")
ORIGINAL_ID = UUID("10000000-0000-4000-8000-000000000003")
PROCESSING_RUN_ID = UUID("10000000-0000-4000-8000-000000000004")
LOCAL_DATABASE_URL = "postgresql+psycopg://test-user:test-password@127.0.0.1:5432/test-database"
PDF_CONTENT = b"%PDF-1.7\nsynthetic route test\n%%EOF\n"


class FakeCaptureDocumentService:
    """Record the HTTP-to-application mapping or raise one configured failure."""

    def __init__(self) -> None:
        self.error: Exception | None = None
        self.calls: list[tuple[WorkspaceContext, CaptureDocumentInput, bytes]] = []

    def capture(
        self,
        context: WorkspaceContext,
        capture_input: CaptureDocumentInput,
        content: BinaryReadable,
    ) -> CaptureDocumentResult:
        """Record a complete stream read and return deterministic identifiers."""

        if self.error is not None:
            raise self.error
        self.calls.append((context, capture_input, content.read()))
        return CaptureDocumentResult(
            document_id=DOCUMENT_ID,
            document_version_id=VERSION_ID,
            original_artifact_id=ORIGINAL_ID,
            processing_run_id=PROCESSING_RUN_ID,
            state=ProcessingState.CAPTURED,
        )


@pytest.fixture
def capture_client(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, FakeCaptureDocumentService]]:
    """Run the real FastAPI boundary with deterministic dependency overrides."""

    application = create_app(
        Settings.model_validate(
            {
                "environment": "test",
                "database_url": LOCAL_DATABASE_URL,
                "object_storage_root": tmp_path / "objects",
                "max_upload_bytes": 1024,
            }
        )
    )
    fake_service = FakeCaptureDocumentService()
    context = WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)
    application.dependency_overrides[get_capture_document_service] = lambda: cast(
        CaptureDocumentService,
        fake_service,
    )
    application.dependency_overrides[get_workspace_context] = lambda: context

    with TestClient(application) as client:
        yield client, fake_service


def valid_form_data() -> dict[str, str]:
    """Return valid English metadata fields for one multipart request."""

    return {
        "title": "Northstar Grid Analytics FY2025 Earnings Brief",
        "language": "en",
        "published_at": "2026-02-19T00:00:00Z",
        "available_at": "2026-02-19T00:00:00Z",
    }


def test_capture_route_maps_multipart_input_and_returns_accepted(
    capture_client: tuple[TestClient, FakeCaptureDocumentService],
) -> None:
    client, service = capture_client

    response = client.post(
        "/api/v1/documents",
        data=valid_form_data(),
        files={"file": ("earnings-brief.pdf", PDF_CONTENT, "application/pdf")},
    )

    assert response.status_code == 202
    assert response.json() == {
        "document_id": str(DOCUMENT_ID),
        "document_version_id": str(VERSION_ID),
        "processing_run_id": str(PROCESSING_RUN_ID),
        "state": "CAPTURED",
        "processing_status_url": f"/api/v1/processing-runs/{PROCESSING_RUN_ID}",
    }
    assert len(service.calls) == 1
    context, capture_input, content = service.calls[0]
    assert context.workspace_id == WORKSPACE_ID
    assert capture_input.filename == "earnings-brief.pdf"
    assert capture_input.media_type == "application/pdf"
    assert capture_input.title == "Northstar Grid Analytics FY2025 Earnings Brief"
    assert content == PDF_CONTENT


def test_client_workspace_field_cannot_change_effective_workspace(
    capture_client: tuple[TestClient, FakeCaptureDocumentService],
) -> None:
    client, service = capture_client
    data = valid_form_data() | {"workspace_id": str(OTHER_WORKSPACE_ID)}

    response = client.post(
        "/api/v1/documents",
        data=data,
        files={"file": ("earnings-brief.pdf", PDF_CONTENT, "application/pdf")},
    )

    assert response.status_code == 202
    assert service.calls[0][0].workspace_id == WORKSPACE_ID


def test_invalid_form_returns_stable_error_without_echoing_input(
    capture_client: tuple[TestClient, FakeCaptureDocumentService],
) -> None:
    client, service = capture_client

    response = client.post(
        "/api/v1/documents",
        data={"title": "   ", "language": "invalid language"},
        files={"file": ("earnings-brief.pdf", PDF_CONTENT, "application/pdf")},
    )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "INVALID_REQUEST",
            "message": "request validation failed",
            "request_id": None,
        }
    }
    assert "invalid language" not in response.text
    assert service.calls == []


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        pytest.param(UploadTooLargeError(), 413, "PAYLOAD_TOO_LARGE", id="file-limit"),
        pytest.param(
            UnsupportedDocumentTypeError(),
            415,
            "UNSUPPORTED_MEDIA_TYPE",
            id="declared-type",
        ),
        pytest.param(
            InvalidPdfSignatureError(),
            415,
            "UNSUPPORTED_MEDIA_TYPE",
            id="signature",
        ),
        pytest.param(ObjectAlreadyExistsError(), 409, "CONFLICT", id="immutable-conflict"),
        pytest.param(ObjectStorageError(), 500, "INTERNAL_ERROR", id="storage"),
        pytest.param(SQLAlchemyError("private database detail"), 500, "INTERNAL_ERROR", id="db"),
    ],
)
def test_capture_route_translates_known_failures(
    capture_client: tuple[TestClient, FakeCaptureDocumentService],
    error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    client, service = capture_client
    service.error = error

    response = client.post(
        "/api/v1/documents",
        data=valid_form_data(),
        files={"file": ("earnings-brief.pdf", PDF_CONTENT, "application/pdf")},
    )

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    assert "private database detail" not in response.text


def test_request_limit_rejects_body_before_capture_service(
    capture_client: tuple[TestClient, FakeCaptureDocumentService],
) -> None:
    client, service = capture_client
    request_limit = 1024 + MULTIPART_OVERHEAD_ALLOWANCE_BYTES

    response = client.post(
        "/api/v1/documents",
        data=valid_form_data(),
        files={"file": ("earnings-brief.pdf", b"x" * request_limit, "application/pdf")},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert service.calls == []
