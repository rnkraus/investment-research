"""Unit tests for the processing-status HTTP boundary."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from investment_research.api.dependencies import (
    get_processing_run_service,
    get_workspace_context,
)
from investment_research.main import create_app
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.application import (
    GetProcessingRunService,
    ProcessingRunUnavailableError,
)
from investment_research.modules.processing.domain import ProcessingRun, ProcessingState
from investment_research.settings import ApiSettings

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
DOCUMENT_VERSION_ID = UUID("10000000-0000-4000-8000-000000000001")
PROCESSING_RUN_ID = UUID("20000000-0000-4000-8000-000000000001")
CREATED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)
LOCAL_DATABASE_URL = "postgresql+psycopg://test-user:test-password@127.0.0.1:5432/test-database"
LOCAL_REDIS_URL = "redis://:synthetic-secret@127.0.0.1:6379/0"


class FakeGetProcessingRunService:
    """Return one configured run or raise one controlled service error."""

    def __init__(self) -> None:
        self.result = make_run()
        self.error: Exception | None = None
        self.calls: list[tuple[WorkspaceContext, UUID]] = []

    def get(self, context: WorkspaceContext, processing_run_id: UUID) -> ProcessingRun:
        """Record trusted input before returning the configured outcome."""

        self.calls.append((context, processing_run_id))
        if self.error is not None:
            raise self.error
        return self.result


def make_context() -> WorkspaceContext:
    """Return the trusted workspace used by route tests."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)


def make_run(*, failed: bool = False) -> ProcessingRun:
    """Build one captured or safely failed processing snapshot."""

    return ProcessingRun(
        id=PROCESSING_RUN_ID,
        workspace_id=WORKSPACE_ID,
        document_version_id=DOCUMENT_VERSION_ID,
        state=ProcessingState.FAILED if failed else ProcessingState.CAPTURED,
        parser_version="synthetic-parser-1",
        parser_config_version="parser-m0.0-v1",
        chunker_version="structure-block-1",
        chunking_config_version="chunking-m0.0-v1",
        lexical_config_version="english-lexical-v1",
        created_at=CREATED_AT,
        started_at=CREATED_AT + timedelta(seconds=1) if failed else None,
        completed_at=CREATED_AT + timedelta(seconds=2) if failed else None,
        error_summary="Document parsing failed safely" if failed else None,
    )


@pytest.fixture
def status_client(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, FakeGetProcessingRunService]]:
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
    fake_service = FakeGetProcessingRunService()
    application.dependency_overrides[get_processing_run_service] = lambda: cast(
        GetProcessingRunService,
        fake_service,
    )
    application.dependency_overrides[get_workspace_context] = make_context

    with TestClient(application) as client:
        yield client, fake_service


def test_processing_status_returns_scoped_snapshot_without_workspace_identifier(
    status_client: tuple[TestClient, FakeGetProcessingRunService],
) -> None:
    client, service = status_client

    response = client.get(f"/api/v1/processing-runs/{PROCESSING_RUN_ID}")

    assert response.status_code == 200
    assert response.json() == {
        "processing_run_id": str(PROCESSING_RUN_ID),
        "document_version_id": str(DOCUMENT_VERSION_ID),
        "state": "CAPTURED",
        "error_summary": None,
    }
    assert response.headers["cache-control"] == "private, no-store"
    assert "workspace" not in response.text
    assert service.calls == [(make_context(), PROCESSING_RUN_ID)]


def test_processing_status_exposes_only_safe_persisted_failure_summary(
    status_client: tuple[TestClient, FakeGetProcessingRunService],
) -> None:
    client, service = status_client
    service.result = make_run(failed=True)

    response = client.get(f"/api/v1/processing-runs/{PROCESSING_RUN_ID}")

    assert response.status_code == 200
    assert response.json()["state"] == "FAILED"
    assert response.json()["error_summary"] == "Document parsing failed safely"


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        pytest.param(ProcessingRunUnavailableError(), 404, "NOT_FOUND", id="unavailable"),
        pytest.param(
            SQLAlchemyError("private database detail"),
            500,
            "INTERNAL_ERROR",
            id="database",
        ),
    ],
)
def test_processing_status_translates_failures_without_internal_details(
    status_client: tuple[TestClient, FakeGetProcessingRunService],
    error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    client, service = status_client
    service.error = error

    response = client.get(f"/api/v1/processing-runs/{PROCESSING_RUN_ID}")

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    assert "private database detail" not in response.text


def test_processing_status_hides_invalid_identifier_value(
    status_client: tuple[TestClient, FakeGetProcessingRunService],
) -> None:
    client, service = status_client

    response = client.get("/api/v1/processing-runs/not-a-uuid")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert "not-a-uuid" not in response.text
    assert service.calls == []
