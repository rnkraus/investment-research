"""Unit tests for trusted execution of versioned processing jobs."""

from dataclasses import dataclass
from typing import cast
from unittest.mock import Mock, create_autospec
from uuid import UUID

import pytest

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.application import (
    ExecuteProcessingJobService,
    ProcessDocumentService,
    ProcessingRunUnavailableError,
)
from investment_research.modules.processing.contracts import ProcessingJobV1
from investment_research.modules.processing.domain import ProcessingRun
from investment_research.modules.processing.ports import ProcessingJobExecutor

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
FOREIGN_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000003")
PROCESSING_RUN_ID = UUID("10000000-0000-4000-8000-000000000004")


@dataclass(frozen=True, slots=True)
class StaticIdentityContextProvider:
    """Return one server-controlled context without accepting job input."""

    context: WorkspaceContext

    def resolve(self) -> WorkspaceContext:
        return self.context


def make_service() -> tuple[ExecuteProcessingJobService, Mock]:
    """Build the job service around a typed processing-service mock."""

    processing_service = create_autospec(ProcessDocumentService, instance=True)
    service = ExecuteProcessingJobService(
        processing_service=cast(ProcessDocumentService, processing_service),
        identity_context_provider=StaticIdentityContextProvider(
            WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)
        ),
    )
    return service, processing_service


def test_matching_job_uses_server_controlled_workspace_context() -> None:
    service, processing_service = make_service()
    job = ProcessingJobV1.create(
        workspace_id=WORKSPACE_ID,
        processing_run_id=PROCESSING_RUN_ID,
    )
    expected = create_autospec(ProcessingRun, instance=True)
    processing_service.process.return_value = expected

    executor: ProcessingJobExecutor = service
    result = executor.execute(job)

    assert result is expected
    processing_service.process.assert_called_once_with(
        WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID),
        PROCESSING_RUN_ID,
    )


def test_foreign_job_is_indistinguishable_from_an_unavailable_run() -> None:
    service, processing_service = make_service()
    foreign_job = ProcessingJobV1.create(
        workspace_id=FOREIGN_WORKSPACE_ID,
        processing_run_id=PROCESSING_RUN_ID,
    )

    with pytest.raises(ProcessingRunUnavailableError, match="processing run is unavailable"):
        service.execute(foreign_job)

    processing_service.process.assert_not_called()
