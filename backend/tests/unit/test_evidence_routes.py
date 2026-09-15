"""Unit tests for the authorized evidence-span HTTP boundary."""

from collections.abc import Iterator
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from investment_research.api.dependencies import (
    get_resolve_evidence_span_service,
    get_workspace_context,
)
from investment_research.main import create_app
from investment_research.modules.evidence.application import (
    EvidenceSpanUnavailableError,
    ResolveEvidenceSpanService,
)
from investment_research.modules.evidence.contracts import ResolvedEvidenceSpan
from investment_research.modules.evidence.domain import EvidenceSpan
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.settings import ApiSettings

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
SPAN_ID = UUID("10000000-0000-4000-8000-000000000001")
DOCUMENT_ID = UUID("10000000-0000-4000-8000-000000000002")
VERSION_ID = UUID("10000000-0000-4000-8000-000000000003")
BLOCK_ID = UUID("10000000-0000-4000-8000-000000000004")
CREATED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)
EVIDENCE_TEXT = "FY2025 revenue was $184.2 million."
LOCAL_DATABASE_URL = "postgresql+psycopg://test-user:test-password@127.0.0.1:5432/test-database"
LOCAL_REDIS_URL = "redis://:synthetic-secret@127.0.0.1:6379/0"


class FakeResolveEvidenceSpanService:
    """Return one configured location and record the trusted lookup input."""

    def __init__(self) -> None:
        self.result = make_result()
        self.error: Exception | None = None
        self.calls: list[tuple[WorkspaceContext, UUID]] = []

    def resolve(
        self,
        context: WorkspaceContext,
        evidence_span_id: UUID,
    ) -> ResolvedEvidenceSpan:
        """Record the call before returning or raising the configured outcome."""

        self.calls.append((context, evidence_span_id))
        if self.error is not None:
            raise self.error
        return self.result


def make_context() -> WorkspaceContext:
    """Return the server-owned workspace used by route tests."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)


def make_result() -> ResolvedEvidenceSpan:
    """Build one authorized exact citation location."""

    return ResolvedEvidenceSpan(
        document_id=DOCUMENT_ID,
        span=EvidenceSpan(
            id=SPAN_ID,
            workspace_id=WORKSPACE_ID,
            document_version_id=VERSION_ID,
            structure_block_id=BLOCK_ID,
            page_number=2,
            start_character=0,
            end_character=len(EVIDENCE_TEXT),
            exact_text=EVIDENCE_TEXT,
            text_sha256=sha256(EVIDENCE_TEXT.encode()).hexdigest(),
            created_at=CREATED_AT,
        ),
    )


@pytest.fixture
def evidence_client(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, FakeResolveEvidenceSpanService]]:
    """Run the real route with controlled service and workspace dependencies."""

    application = create_app(
        ApiSettings.model_validate(
            {
                "environment": "test",
                "database_url": LOCAL_DATABASE_URL,
                "redis_url": LOCAL_REDIS_URL,
                "object_storage_root": tmp_path / "objects",
            }
        )
    )
    fake_service = FakeResolveEvidenceSpanService()
    application.dependency_overrides[get_resolve_evidence_span_service] = lambda: cast(
        ResolveEvidenceSpanService,
        fake_service,
    )
    application.dependency_overrides[get_workspace_context] = make_context

    with TestClient(application) as client:
        yield client, fake_service


def test_evidence_route_returns_exact_location_and_protected_viewer_target(
    evidence_client: tuple[TestClient, FakeResolveEvidenceSpanService],
) -> None:
    client, service = evidence_client

    response = client.get(f"/api/v1/evidence-spans/{SPAN_ID}")
    expected_viewer_url = f"/api/v1/documents/{DOCUMENT_ID}/versions/{VERSION_ID}/original#page=2"

    assert response.status_code == 200
    assert response.json() == {
        "evidence_span_id": str(SPAN_ID),
        "document_id": str(DOCUMENT_ID),
        "document_version_id": str(VERSION_ID),
        "page": 2,
        "start_character": 0,
        "end_character": len(EVIDENCE_TEXT),
        "exact_text": EVIDENCE_TEXT,
        "text_sha256": sha256(EVIDENCE_TEXT.encode()).hexdigest(),
        "viewer_url": expected_viewer_url,
    }
    assert response.headers["cache-control"] == "private, no-store"
    assert "workspace" not in response.text
    assert "storage_key" not in response.text
    assert service.calls == [(make_context(), SPAN_ID)]


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        pytest.param(EvidenceSpanUnavailableError(), 404, "NOT_FOUND", id="unavailable"),
        pytest.param(
            SQLAlchemyError("private database detail"),
            500,
            "INTERNAL_ERROR",
            id="database",
        ),
    ],
)
def test_evidence_route_translates_failures_without_internal_details(
    evidence_client: tuple[TestClient, FakeResolveEvidenceSpanService],
    error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    client, service = evidence_client
    service.error = error

    response = client.get(f"/api/v1/evidence-spans/{SPAN_ID}")

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    assert "private database detail" not in response.text


def test_evidence_route_hides_invalid_identifier_before_service_call(
    evidence_client: tuple[TestClient, FakeResolveEvidenceSpanService],
) -> None:
    client, service = evidence_client

    response = client.get("/api/v1/evidence-spans/not-a-uuid")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert "not-a-uuid" not in response.text
    assert service.calls == []
