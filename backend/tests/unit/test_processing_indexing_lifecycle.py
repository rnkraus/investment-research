"""Unit tests for canonical processing transitions around dense indexing."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import Mock, create_autospec
from uuid import UUID

import pytest

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.application import (
    M1_PROCESSING_PROFILE,
    ProcessingRetryableError,
    ProcessingRunAlreadyActiveError,
)
from investment_research.modules.processing.domain import (
    Chunk,
    ProcessingRun,
    ProcessingState,
)
from investment_research.modules.processing.indexing import IndexProcessingRunService
from investment_research.modules.processing.ports import (
    ProcessingRepository,
    ProcessingUnitOfWork,
)
from investment_research.modules.search.indexing import (
    DenseIndexingInputError,
    DocumentIndexingResult,
    IndexActiveChunksService,
)
from investment_research.modules.search.ports import SearchProviderTransientError

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
PROCESSING_RUN_ID = UUID("10000000-0000-4000-8000-000000000001")
DOCUMENT_VERSION_ID = UUID("20000000-0000-4000-8000-000000000001")
CHUNK_SET_ID = UUID("30000000-0000-4000-8000-000000000001")
STRUCTURE_BLOCK_ID = UUID("40000000-0000-4000-8000-000000000001")
CHUNK_ID = UUID("50000000-0000-4000-8000-000000000001")
LEASE_ID = UUID("60000000-0000-4000-8000-000000000001")
RECLAIMED_LEASE_ID = UUID("60000000-0000-4000-8000-000000000002")
CREATED_AT = datetime(2026, 9, 18, 12, tzinfo=UTC)
STARTED_AT = CREATED_AT + timedelta(seconds=1)
NOW = STARTED_AT + timedelta(minutes=7)
COMPLETED_AT = NOW + timedelta(seconds=1)


def make_context() -> WorkspaceContext:
    """Build the trusted workspace used by lifecycle tests."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)


def make_run(
    state: ProcessingState = ProcessingState.LEXICAL_READY,
    *,
    attempt_count: int = 1,
    lease_expires_at: datetime | None = None,
) -> ProcessingRun:
    """Build one active M1 run at a dense-index lifecycle stage."""

    return ProcessingRun(
        id=PROCESSING_RUN_ID,
        workspace_id=WORKSPACE_ID,
        document_version_id=DOCUMENT_VERSION_ID,
        state=state,
        parser_version=M1_PROCESSING_PROFILE.parser_version,
        parser_config_version=M1_PROCESSING_PROFILE.parser_config_version,
        chunker_version=M1_PROCESSING_PROFILE.chunker_version,
        chunking_config_version=M1_PROCESSING_PROFILE.chunking_config_version,
        lexical_config_version=M1_PROCESSING_PROFILE.lexical_config_version,
        embedding_config_version=M1_PROCESSING_PROFILE.embedding_config_version,
        dense_index_config_version=M1_PROCESSING_PROFILE.dense_index_config_version,
        created_at=CREATED_AT,
        attempt_count=attempt_count,
        lease_id=LEASE_ID,
        lease_expires_at=lease_expires_at or NOW + timedelta(minutes=6),
        started_at=STARTED_AT,
    )


def make_chunk() -> Chunk:
    """Build one canonical active chunk for the processing run."""

    return Chunk(
        id=CHUNK_ID,
        workspace_id=WORKSPACE_ID,
        processing_run_id=PROCESSING_RUN_ID,
        document_version_id=DOCUMENT_VERSION_ID,
        chunk_set_id=CHUNK_SET_ID,
        structure_block_id=STRUCTURE_BLOCK_ID,
        ordinal=0,
        text="Canonical revenue evidence.",
        token_count=3,
        created_at=CREATED_AT,
    )


class InMemoryProcessingRepository:
    """Store one canonical run and expose stable active chunks."""

    def __init__(self, run: ProcessingRun, chunks: Sequence[Chunk] = (make_chunk(),)) -> None:
        self.run = run
        self.chunks = tuple(chunks)
        self.updates: list[ProcessingRun] = []
        self.active_chunk_reads = 0

    def add_run(self, context: WorkspaceContext, run: ProcessingRun) -> None:
        raise AssertionError("indexing must not create processing runs")

    def get_run_by_id(
        self,
        context: WorkspaceContext,
        processing_run_id: UUID,
    ) -> ProcessingRun | None:
        return self._scoped_run(context, processing_run_id)

    def get_run_by_id_for_update(
        self,
        context: WorkspaceContext,
        processing_run_id: UUID,
    ) -> ProcessingRun | None:
        return self._scoped_run(context, processing_run_id)

    def update_run(self, context: WorkspaceContext, run: ProcessingRun) -> bool:
        if self._scoped_run(context, run.id) is None:
            return False
        self.run = run
        self.updates.append(run)
        return True

    def list_active_chunks(
        self,
        context: WorkspaceContext,
        processing_run_id: UUID,
    ) -> Sequence[Chunk]:
        if self._scoped_run(context, processing_run_id) is None:
            return ()
        self.active_chunk_reads += 1
        return self.chunks

    def add_lexical_output(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("indexing must not replace canonical lexical output")

    def _scoped_run(
        self,
        context: WorkspaceContext,
        processing_run_id: UUID,
    ) -> ProcessingRun | None:
        if context.workspace_id != self.run.workspace_id or processing_run_id != self.run.id:
            return None
        return self.run


@dataclass(frozen=True, slots=True)
class RecordingUnitOfWork:
    """Expose the in-memory repository through the canonical unit-of-work port."""

    processing: ProcessingRepository


class RecordingUnitOfWorkFactory:
    """Count transaction scopes opened around provider work."""

    def __init__(self, repository: ProcessingRepository) -> None:
        self._repository = repository
        self.transaction_count = 0

    @contextmanager
    def open(self) -> Iterator[ProcessingUnitOfWork]:
        self.transaction_count += 1
        yield RecordingUnitOfWork(processing=self._repository)


def make_service(
    repository: InMemoryProcessingRepository,
    *,
    indexing_error: Exception | None = None,
) -> tuple[IndexProcessingRunService, Mock, RecordingUnitOfWorkFactory]:
    """Build one lifecycle service around a typed indexing-service mock."""

    indexing_service = create_autospec(IndexActiveChunksService, instance=True)
    indexing_service.index.return_value = DocumentIndexingResult(
        vector_ids=("v1:" + "a" * 64,),
        batch_count=1,
    )
    indexing_service.index.side_effect = indexing_error
    unit_of_work_factory = RecordingUnitOfWorkFactory(repository)
    timestamps = iter((NOW, COMPLETED_AT))
    service = IndexProcessingRunService(
        indexing_service=cast(IndexActiveChunksService, indexing_service),
        unit_of_work_factory=unit_of_work_factory.open,
        clock=lambda: next(timestamps),
        lease_identifier_factory=lambda: RECLAIMED_LEASE_ID,
    )
    return service, indexing_service, unit_of_work_factory


def test_owned_lexical_ready_run_indexes_outside_transactions_and_becomes_searchable() -> None:
    repository = InMemoryProcessingRepository(make_run())
    service, indexing_service, unit_of_work_factory = make_service(repository)

    result = service.index(
        make_context(),
        PROCESSING_RUN_ID,
        owned_lease_id=LEASE_ID,
    )

    assert result.state is ProcessingState.SEARCHABLE
    assert result.completed_at == COMPLETED_AT
    assert result.lease_id is None
    assert [run.state for run in repository.updates] == [
        ProcessingState.INDEXING,
        ProcessingState.SEARCHABLE,
    ]
    assert unit_of_work_factory.transaction_count == 2
    indexing_run = repository.updates[0]
    indexing_service.index.assert_called_once_with(
        make_context(),
        indexing_run,
        (make_chunk(),),
    )


def test_expired_indexing_run_reclaims_lease_and_reuses_canonical_chunks() -> None:
    repository = InMemoryProcessingRepository(
        make_run(
            ProcessingState.INDEXING,
            lease_expires_at=NOW,
        )
    )
    service, indexing_service, _ = make_service(repository)

    result = service.index(make_context(), PROCESSING_RUN_ID)

    reclaimed_run = repository.updates[0]
    assert reclaimed_run.state is ProcessingState.INDEXING
    assert reclaimed_run.attempt_count == 2
    assert reclaimed_run.lease_id == RECLAIMED_LEASE_ID
    assert result.state is ProcessingState.SEARCHABLE
    assert repository.active_chunk_reads == 1
    indexing_service.index.assert_called_once_with(
        make_context(),
        reclaimed_run,
        (make_chunk(),),
    )


def test_live_indexing_lease_cannot_be_taken_by_another_execution() -> None:
    repository = InMemoryProcessingRepository(make_run())
    service, indexing_service, unit_of_work_factory = make_service(repository)

    with pytest.raises(ProcessingRunAlreadyActiveError) as captured:
        service.index(make_context(), PROCESSING_RUN_ID)

    assert captured.value.retry_at == NOW + timedelta(minutes=6)
    assert repository.updates == []
    assert unit_of_work_factory.transaction_count == 1
    indexing_service.index.assert_not_called()


def test_transient_indexing_failure_expires_lease_and_requests_retry() -> None:
    repository = InMemoryProcessingRepository(make_run())
    service, _, unit_of_work_factory = make_service(
        repository,
        indexing_error=SearchProviderTransientError("sensitive provider detail"),
    )

    with pytest.raises(ProcessingRetryableError, match="temporarily unavailable") as captured:
        service.index(
            make_context(),
            PROCESSING_RUN_ID,
            owned_lease_id=LEASE_ID,
        )

    assert captured.value.__cause__ is None
    assert "sensitive" not in str(captured.value)
    assert repository.run.state is ProcessingState.INDEXING
    assert repository.run.lease_id == LEASE_ID
    assert repository.run.lease_expires_at == COMPLETED_AT
    assert repository.run.completed_at is None
    assert unit_of_work_factory.transaction_count == 2


def test_transient_failure_at_attempt_limit_becomes_terminal() -> None:
    repository = InMemoryProcessingRepository(make_run(attempt_count=3))
    provider_error = SearchProviderTransientError("sensitive provider detail")
    service, _, unit_of_work_factory = make_service(
        repository,
        indexing_error=provider_error,
    )

    with pytest.raises(SearchProviderTransientError) as captured:
        service.index(
            make_context(),
            PROCESSING_RUN_ID,
            owned_lease_id=LEASE_ID,
        )

    assert captured.value is provider_error
    assert repository.run.state is ProcessingState.FAILED
    assert repository.run.error_summary == "Processing attempt limit exceeded"
    assert repository.run.completed_at == COMPLETED_AT
    assert unit_of_work_factory.transaction_count == 2


def test_permanent_indexing_failure_records_only_safe_summary() -> None:
    repository = InMemoryProcessingRepository(make_run())
    indexing_error = DenseIndexingInputError("sensitive canonical detail")
    service, _, _ = make_service(repository, indexing_error=indexing_error)

    with pytest.raises(DenseIndexingInputError) as captured:
        service.index(
            make_context(),
            PROCESSING_RUN_ID,
            owned_lease_id=LEASE_ID,
        )

    assert captured.value is indexing_error
    assert repository.run.state is ProcessingState.FAILED
    assert repository.run.error_summary == "Dense indexing failed safely"
    assert "sensitive" not in repository.run.error_summary


@pytest.mark.parametrize("state", [ProcessingState.SEARCHABLE, ProcessingState.FAILED])
def test_terminal_indexing_delivery_is_an_idempotent_noop(state: ProcessingState) -> None:
    active = make_run(ProcessingState.INDEXING)
    terminal = (
        active.mark_searchable(lease_id=LEASE_ID, completed_at=COMPLETED_AT)
        if state is ProcessingState.SEARCHABLE
        else active.mark_failed(
            lease_id=LEASE_ID,
            completed_at=COMPLETED_AT,
            error_summary="Dense indexing failed safely",
        )
    )
    repository = InMemoryProcessingRepository(terminal)
    service, indexing_service, unit_of_work_factory = make_service(repository)

    result = service.index(make_context(), PROCESSING_RUN_ID)

    assert result is terminal
    assert repository.updates == []
    assert unit_of_work_factory.transaction_count == 1
    indexing_service.index.assert_not_called()
