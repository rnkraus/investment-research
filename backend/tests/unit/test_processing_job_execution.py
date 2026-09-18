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
    ProcessingAttemptResult,
    ProcessingRunUnavailableError,
)
from investment_research.modules.processing.contracts import ProcessingJobV1
from investment_research.modules.processing.domain import ProcessingRun, ProcessingState
from investment_research.modules.processing.ports import (
    ProcessingJobExecutor,
    ProcessingRunIndexer,
)

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
FOREIGN_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000003")
PROCESSING_RUN_ID = UUID("10000000-0000-4000-8000-000000000004")
LEASE_ID = UUID("20000000-0000-4000-8000-000000000005")


@dataclass(frozen=True, slots=True)
class StaticIdentityContextProvider:
    """Return one server-controlled context without accepting job input."""

    context: WorkspaceContext

    def resolve(self) -> WorkspaceContext:
        return self.context


def make_service() -> tuple[ExecuteProcessingJobService, Mock, Mock]:
    """Build the job service around typed parsing and indexing mocks."""

    processing_service = create_autospec(ProcessDocumentService, instance=True)
    indexing_service = create_autospec(ProcessingRunIndexer, instance=True)
    service = ExecuteProcessingJobService(
        processing_service=cast(ProcessDocumentService, processing_service),
        indexing_service=cast(ProcessingRunIndexer, indexing_service),
        identity_context_provider=StaticIdentityContextProvider(
            WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)
        ),
    )
    return service, processing_service, indexing_service


def test_matching_job_uses_server_controlled_workspace_context() -> None:
    service, processing_service, indexing_service = make_service()
    job = ProcessingJobV1.create(
        workspace_id=WORKSPACE_ID,
        processing_run_id=PROCESSING_RUN_ID,
    )
    expected = create_autospec(ProcessingRun, instance=True)
    expected.dense_indexing_enabled = False
    expected.state = ProcessingState.LEXICAL_READY
    processing_service.process_attempt.return_value = ProcessingAttemptResult(
        run=expected,
        owned_indexing_lease_id=None,
    )

    executor: ProcessingJobExecutor = service
    result = executor.execute(job)

    assert result is expected
    processing_service.process_attempt.assert_called_once_with(
        WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID),
        PROCESSING_RUN_ID,
    )
    indexing_service.index.assert_not_called()


def test_freshly_parsed_dense_job_hands_its_owned_lease_to_indexing() -> None:
    service, processing_service, indexing_service = make_service()
    job = ProcessingJobV1.create(
        workspace_id=WORKSPACE_ID,
        processing_run_id=PROCESSING_RUN_ID,
    )
    ready_run = create_autospec(ProcessingRun, instance=True)
    ready_run.dense_indexing_enabled = True
    ready_run.state = ProcessingState.LEXICAL_READY
    ready_run.id = PROCESSING_RUN_ID
    searchable_run = create_autospec(ProcessingRun, instance=True)
    processing_service.process_attempt.return_value = ProcessingAttemptResult(
        run=ready_run,
        owned_indexing_lease_id=LEASE_ID,
    )
    indexing_service.index.return_value = searchable_run

    result = service.execute(job)

    assert result is searchable_run
    processing_service.process_attempt.assert_called_once()
    indexing_service.index.assert_called_once_with(
        WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID),
        PROCESSING_RUN_ID,
        owned_lease_id=LEASE_ID,
    )


def test_dense_job_fails_closed_when_no_indexing_service_is_wired() -> None:
    service, processing_service, _ = make_service()
    service = ExecuteProcessingJobService(
        processing_service=service.processing_service,
        indexing_service=None,
        identity_context_provider=service.identity_context_provider,
    )
    job = ProcessingJobV1.create(
        workspace_id=WORKSPACE_ID,
        processing_run_id=PROCESSING_RUN_ID,
    )
    ready_run = create_autospec(ProcessingRun, instance=True)
    ready_run.dense_indexing_enabled = True
    ready_run.state = ProcessingState.LEXICAL_READY
    ready_run.id = PROCESSING_RUN_ID
    processing_service.process_attempt.return_value = ProcessingAttemptResult(
        run=ready_run,
        owned_indexing_lease_id=LEASE_ID,
    )

    with pytest.raises(RuntimeError, match="dense indexing service is required"):
        service.execute(job)


@pytest.mark.parametrize(
    "state",
    [ProcessingState.LEXICAL_READY, ProcessingState.INDEXING],
)
def test_preexisting_dense_stage_never_inherits_another_deliverys_lease(
    state: ProcessingState,
) -> None:
    service, processing_service, indexing_service = make_service()
    job = ProcessingJobV1.create(
        workspace_id=WORKSPACE_ID,
        processing_run_id=PROCESSING_RUN_ID,
    )
    active_run = create_autospec(ProcessingRun, instance=True)
    active_run.dense_indexing_enabled = True
    active_run.state = state
    active_run.id = PROCESSING_RUN_ID
    searchable_run = create_autospec(ProcessingRun, instance=True)
    processing_service.process_attempt.return_value = ProcessingAttemptResult(
        run=active_run,
        owned_indexing_lease_id=None,
    )
    indexing_service.index.return_value = searchable_run

    assert service.execute(job) is searchable_run
    indexing_service.index.assert_called_once_with(
        WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID),
        PROCESSING_RUN_ID,
        owned_lease_id=None,
    )


@pytest.mark.parametrize(
    "state",
    [ProcessingState.SEARCHABLE, ProcessingState.FAILED],
)
def test_terminal_dense_job_is_an_idempotent_noop(state: ProcessingState) -> None:
    service, processing_service, indexing_service = make_service()
    job = ProcessingJobV1.create(
        workspace_id=WORKSPACE_ID,
        processing_run_id=PROCESSING_RUN_ID,
    )
    terminal_run = create_autospec(ProcessingRun, instance=True)
    terminal_run.dense_indexing_enabled = True
    terminal_run.state = state
    processing_service.process_attempt.return_value = ProcessingAttemptResult(
        run=terminal_run,
        owned_indexing_lease_id=None,
    )

    assert service.execute(job) is terminal_run
    indexing_service.index.assert_not_called()


def test_foreign_job_is_indistinguishable_from_an_unavailable_run() -> None:
    service, processing_service, indexing_service = make_service()
    foreign_job = ProcessingJobV1.create(
        workspace_id=FOREIGN_WORKSPACE_ID,
        processing_run_id=PROCESSING_RUN_ID,
    )

    with pytest.raises(ProcessingRunUnavailableError, match="processing run is unavailable"):
        service.execute(foreign_job)

    processing_service.process_attempt.assert_not_called()
    indexing_service.index.assert_not_called()
