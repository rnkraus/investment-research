"""PostgreSQL integration tests for the single-document processing service."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from typing import BinaryIO
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, select

from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.infrastructure.database.unit_of_work import (
    create_capture_unit_of_work_factory,
    create_document_read_unit_of_work_factory,
    create_processing_unit_of_work_factory,
)
from investment_research.infrastructure.object_storage.local import LocalObjectStorage
from investment_research.infrastructure.parsing.pypdf import PypdfDocumentParser
from investment_research.modules.documents.application import (
    CaptureDocumentService,
    OpenOriginalService,
)
from investment_research.modules.documents.contracts import (
    CaptureDocumentInput,
    CaptureDocumentResult,
)
from investment_research.modules.documents.persistence import SqlAlchemyDocumentRepository
from investment_research.modules.identity.domain import (
    Workspace,
    WorkspaceContext,
    WorkspaceType,
)
from investment_research.modules.identity.persistence import SqlAlchemyWorkspaceRepository
from investment_research.modules.processing.application import (
    M0_0_PROCESSING_PROFILE,
    ProcessDocumentService,
    ProcessingProfile,
    ProcessingRetryableError,
    ProcessingRunAlreadyActiveError,
    ProcessingRunConflictError,
    ProcessingRunUnavailableError,
)
from investment_research.modules.processing.contracts import ParsedDocument, ParserLimits
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.processing.persistence import (
    ChunkRow,
    ChunkSetRow,
    ProcessingRunRow,
    SqlAlchemyProcessingRepository,
    StructureBlockRow,
)
from investment_research.modules.processing.ports import (
    DocumentParser,
    ParserLimitExceededError,
    ParserTransportError,
)
from tests.fakes import RecordingProcessingDispatcher

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_PDF = (
    REPOSITORY_ROOT / "fixtures/m0_0/documents/northstar-grid-analytics-fy2025-earnings-brief.pdf"
)
CAPTURED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)
STARTED_AT = CAPTURED_AT + timedelta(seconds=1)
COMPLETED_AT = CAPTURED_AT + timedelta(seconds=2)


def create_workspace(engine: Engine) -> WorkspaceContext:
    """Persist one unique synthetic workspace for a processing attempt."""

    workspace_id = uuid4()
    context = WorkspaceContext(user_id=uuid4(), workspace_id=workspace_id)
    workspace = Workspace(
        id=workspace_id,
        name=f"Processing Service Workspace {workspace_id}",
        workspace_type=WorkspaceType.SANITIZED_DEMO,
        created_at=CAPTURED_AT,
    )
    session_factory = create_session_factory(engine)
    with transactional_session(session_factory) as session:
        SqlAlchemyWorkspaceRepository(session).add(context, workspace)
    return context


def capture_fixture(
    engine: Engine,
    context: WorkspaceContext,
    storage: LocalObjectStorage,
) -> CaptureDocumentResult:
    """Capture the public fixture through the same service used by the API."""

    session_factory = create_session_factory(engine)
    service = CaptureDocumentService(
        object_storage=storage,
        unit_of_work_factory=create_capture_unit_of_work_factory(session_factory),
        max_upload_bytes=FIXTURE_PDF.stat().st_size + 1,
        processing_dispatcher=RecordingProcessingDispatcher(),
        clock=lambda: CAPTURED_AT,
    )
    capture_input = CaptureDocumentInput(
        filename=FIXTURE_PDF.name,
        media_type="application/pdf",
        title="Northstar Grid Analytics FY2025 Earnings Brief",
        language="en",
        published_at=CAPTURED_AT,
        available_at=CAPTURED_AT,
    )
    with FIXTURE_PDF.open("rb") as content:
        result = service.capture(context, capture_input, content)
    return result


def processing_clock() -> Iterator[datetime]:
    """Yield the deterministic start and terminal timestamps for one run."""

    yield from (STARTED_AT, COMPLETED_AT)


def make_processing_service(
    engine: Engine,
    storage: LocalObjectStorage,
    *,
    limits: ParserLimits,
    parser: DocumentParser | None = None,
    timestamps: Iterator[datetime] | None = None,
    profile: ProcessingProfile = M0_0_PROCESSING_PROFILE,
) -> ProcessDocumentService:
    """Wire real M0.0 adapters around the processing application service."""

    session_factory = create_session_factory(engine)
    resolved_timestamps = timestamps or processing_clock()
    return ProcessDocumentService(
        parser=parser or PypdfDocumentParser(),
        original_provider=OpenOriginalService(
            object_storage=storage,
            unit_of_work_factory=create_document_read_unit_of_work_factory(session_factory),
        ),
        unit_of_work_factory=create_processing_unit_of_work_factory(session_factory),
        parser_limits=limits,
        profile=profile,
        clock=lambda: next(resolved_timestamps),
    )


class BlockingParser:
    """Pause a real parser so another delivery can exercise lease behavior."""

    def __init__(self) -> None:
        self._delegate = PypdfDocumentParser()
        self.entered = Event()
        self.release = Event()

    @property
    def parser_version(self) -> str:
        """Expose the delegate version recorded by the captured processing run."""

        return self._delegate.parser_version

    def parse(self, content: BinaryIO, *, limits: ParserLimits) -> ParsedDocument:
        """Wait for the test before parsing the caller-owned original stream."""

        self.entered.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("blocking parser was not released by the test")
        return self._delegate.parse(content, limits=limits)


class FailOnceParser:
    """Simulate one transient parser transport failure before recovery."""

    def __init__(self) -> None:
        self._delegate = PypdfDocumentParser()
        self.calls = 0

    @property
    def parser_version(self) -> str:
        """Expose the same reproducible version as the real parser."""

        return self._delegate.parser_version

    def parse(self, content: BinaryIO, *, limits: ParserLimits) -> ParsedDocument:
        """Fail the first delivery and parse normally on the retry."""

        self.calls += 1
        if self.calls == 1:
            raise ParserTransportError("synthetic private transport failure")
        return self._delegate.parse(content, limits=limits)


def test_process_fixture_commits_searchable_output_and_ready_state(
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    context = create_workspace(integration_database_engine)
    storage = LocalObjectStorage(tmp_path / "objects")
    capture = capture_fixture(integration_database_engine, context, storage)
    service = make_processing_service(
        integration_database_engine,
        storage,
        limits=ParserLimits(max_pages=10, max_extracted_characters=100_000),
    )

    result = service.process(context, capture.processing_run_id)

    assert result.state is ProcessingState.LEXICAL_READY
    assert result.started_at == STARTED_AT
    assert result.completed_at == COMPLETED_AT
    session_factory = create_session_factory(integration_database_engine)
    with transactional_session(session_factory) as session:
        persisted_run = SqlAlchemyProcessingRepository(session).get_run_by_id(
            context,
            capture.processing_run_id,
        )
        blocks = tuple(
            session.scalars(
                select(StructureBlockRow)
                .where(StructureBlockRow.processing_run_id == capture.processing_run_id)
                .order_by(StructureBlockRow.ordinal)
            )
        )
        chunk_sets = tuple(
            session.scalars(
                select(ChunkSetRow).where(
                    ChunkSetRow.processing_run_id == capture.processing_run_id
                )
            )
        )
        chunks = tuple(
            session.scalars(
                select(ChunkRow)
                .where(ChunkRow.processing_run_id == capture.processing_run_id)
                .order_by(ChunkRow.ordinal)
            )
        )
        revenue_matches = session.scalar(
            select(func.count())
            .select_from(ChunkRow)
            .where(
                ChunkRow.processing_run_id == capture.processing_run_id,
                ChunkRow.search_vector.bool_op("@@")(func.plainto_tsquery("english", "revenue")),
            )
        )
        block_page_numbers = tuple(block.page_number for block in blocks)
        chunk_set_count = len(chunk_sets)
        chunk_count = len(chunks)

    assert persisted_run == result
    assert block_page_numbers == (1, 2, 3, 4)
    assert chunk_set_count == 1
    assert chunk_count == 4
    assert revenue_matches is not None and revenue_matches > 0


def test_parser_limit_failure_preserves_original_and_records_failed_state(
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    context = create_workspace(integration_database_engine)
    storage = LocalObjectStorage(tmp_path / "objects")
    capture = capture_fixture(integration_database_engine, context, storage)
    service = make_processing_service(
        integration_database_engine,
        storage,
        limits=ParserLimits(max_pages=3, max_extracted_characters=100_000),
    )

    with pytest.raises(ParserLimitExceededError, match="page limit"):
        service.process(context, capture.processing_run_id)

    session_factory = create_session_factory(integration_database_engine)
    with transactional_session(session_factory) as session:
        processing = SqlAlchemyProcessingRepository(session)
        documents = SqlAlchemyDocumentRepository(session)
        persisted_run = processing.get_run_by_id(context, capture.processing_run_id)
        original = documents.get_original_artifact(
            context,
            capture.document_id,
            capture.document_version_id,
        )
        structure_block_count = session.scalar(
            select(func.count())
            .select_from(StructureBlockRow)
            .where(StructureBlockRow.processing_run_id == capture.processing_run_id)
        )
        chunk_set_count = session.scalar(
            select(func.count())
            .select_from(ChunkSetRow)
            .where(ChunkSetRow.processing_run_id == capture.processing_run_id)
        )
        chunk_count = session.scalar(
            select(func.count())
            .select_from(ChunkRow)
            .where(ChunkRow.processing_run_id == capture.processing_run_id)
        )

    assert persisted_run is not None
    assert persisted_run.state is ProcessingState.FAILED
    assert persisted_run.error_summary == "Parser safety limit exceeded"
    assert original is not None
    with storage.open(context, original.storage_key) as content:
        assert content.read() == FIXTURE_PDF.read_bytes()
    assert structure_block_count == 0
    assert chunk_set_count == 0
    assert chunk_count == 0


def test_transient_parser_failure_expires_lease_and_retry_commits_once(
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    context = create_workspace(integration_database_engine)
    storage = LocalObjectStorage(tmp_path / "objects")
    capture = capture_fixture(integration_database_engine, context, storage)
    parser = FailOnceParser()
    first_failure_at = STARTED_AT + timedelta(seconds=1)
    retry_started_at = STARTED_AT + timedelta(seconds=2)
    retry_completed_at = STARTED_AT + timedelta(seconds=3)
    service = make_processing_service(
        integration_database_engine,
        storage,
        limits=ParserLimits(max_pages=10, max_extracted_characters=100_000),
        parser=parser,
        timestamps=iter(
            (
                STARTED_AT,
                first_failure_at,
                retry_started_at,
                retry_completed_at,
            )
        ),
    )

    with pytest.raises(
        ProcessingRetryableError,
        match="processing dependency is temporarily unavailable",
    ):
        service.process(context, capture.processing_run_id)

    retried_result = service.process(context, capture.processing_run_id)

    session_factory = create_session_factory(integration_database_engine)
    with transactional_session(session_factory) as session:
        persisted_run = SqlAlchemyProcessingRepository(session).get_run_by_id(
            context,
            capture.processing_run_id,
        )
        chunk_set_count = session.scalar(
            select(func.count())
            .select_from(ChunkSetRow)
            .where(ChunkSetRow.processing_run_id == capture.processing_run_id)
        )

    assert parser.calls == 2
    assert retried_result.state is ProcessingState.LEXICAL_READY
    assert retried_result.attempt_count == 2
    assert retried_result.completed_at == retry_completed_at
    assert persisted_run == retried_result
    assert chunk_set_count == 1


def test_transient_parser_failure_at_attempt_limit_records_failed_state(
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    context = create_workspace(integration_database_engine)
    storage = LocalObjectStorage(tmp_path / "objects")
    capture = capture_fixture(integration_database_engine, context, storage)
    service = make_processing_service(
        integration_database_engine,
        storage,
        limits=ParserLimits(max_pages=10, max_extracted_characters=100_000),
        parser=FailOnceParser(),
        profile=replace(M0_0_PROCESSING_PROFILE, maximum_attempts=1),
    )

    with pytest.raises(ParserTransportError, match="synthetic private transport failure"):
        service.process(context, capture.processing_run_id)

    session_factory = create_session_factory(integration_database_engine)
    with transactional_session(session_factory) as session:
        persisted_run = SqlAlchemyProcessingRepository(session).get_run_by_id(
            context,
            capture.processing_run_id,
        )
        derived_row_count = session.scalar(
            select(func.count())
            .select_from(StructureBlockRow)
            .where(StructureBlockRow.processing_run_id == capture.processing_run_id)
        )

    assert persisted_run is not None
    assert persisted_run.state is ProcessingState.FAILED
    assert persisted_run.attempt_count == 1
    assert persisted_run.completed_at == COMPLETED_AT
    assert persisted_run.error_summary == "Processing attempt limit exceeded"
    assert derived_row_count == 0


def test_processing_service_does_not_disclose_cross_workspace_run(
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    owner_context = create_workspace(integration_database_engine)
    foreign_context = create_workspace(integration_database_engine)
    storage = LocalObjectStorage(tmp_path / "objects")
    capture = capture_fixture(integration_database_engine, owner_context, storage)
    service = make_processing_service(
        integration_database_engine,
        storage,
        limits=ParserLimits(max_pages=10, max_extracted_characters=100_000),
    )

    with pytest.raises(ProcessingRunUnavailableError, match="processing run is unavailable"):
        service.process(foreign_context, capture.processing_run_id)

    session_factory = create_session_factory(integration_database_engine)
    with transactional_session(session_factory) as session:
        row = session.get(ProcessingRunRow, capture.processing_run_id)
        row_state = None if row is None else row.state
    assert row_state is ProcessingState.CAPTURED


def test_concurrent_delivery_cannot_steal_an_active_lease(
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    context = create_workspace(integration_database_engine)
    storage = LocalObjectStorage(tmp_path / "objects")
    capture = capture_fixture(integration_database_engine, context, storage)
    limits = ParserLimits(max_pages=10, max_extracted_characters=100_000)
    blocking_parser = BlockingParser()
    first_service = make_processing_service(
        integration_database_engine,
        storage,
        limits=limits,
        parser=blocking_parser,
    )
    duplicate_at = STARTED_AT + timedelta(seconds=1)
    second_service = make_processing_service(
        integration_database_engine,
        storage,
        limits=limits,
        timestamps=iter((duplicate_at,)),
    )
    first_results: list[ProcessingState] = []
    first_errors: list[Exception] = []

    def execute_first_delivery() -> None:
        try:
            result = first_service.process(context, capture.processing_run_id)
            first_results.append(result.state)
        except Exception as error:
            first_errors.append(error)

    first_thread = Thread(target=execute_first_delivery)
    first_thread.start()
    assert blocking_parser.entered.wait(timeout=5)
    try:
        with pytest.raises(ProcessingRunAlreadyActiveError, match="active attempt") as error:
            second_service.process(context, capture.processing_run_id)
        assert error.value.retry_at == STARTED_AT + M0_0_PROCESSING_PROFILE.lease_duration
    finally:
        blocking_parser.release.set()
        first_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert first_errors == []
    assert first_results == [ProcessingState.LEXICAL_READY]


def test_expired_lease_is_reclaimed_and_stale_worker_cannot_commit(
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    context = create_workspace(integration_database_engine)
    storage = LocalObjectStorage(tmp_path / "objects")
    capture = capture_fixture(integration_database_engine, context, storage)
    limits = ParserLimits(max_pages=10, max_extracted_characters=100_000)
    blocking_parser = BlockingParser()
    first_service = make_processing_service(
        integration_database_engine,
        storage,
        limits=limits,
        parser=blocking_parser,
    )
    reclaim_at = STARTED_AT + M0_0_PROCESSING_PROFILE.lease_duration
    reclaimed_service = make_processing_service(
        integration_database_engine,
        storage,
        limits=limits,
        timestamps=iter((reclaim_at, reclaim_at + timedelta(seconds=1))),
    )
    stale_errors: list[Exception] = []

    def execute_stale_delivery() -> None:
        try:
            first_service.process(context, capture.processing_run_id)
        except Exception as error:
            stale_errors.append(error)

    stale_thread = Thread(target=execute_stale_delivery)
    stale_thread.start()
    assert blocking_parser.entered.wait(timeout=5)
    try:
        reclaimed_result = reclaimed_service.process(context, capture.processing_run_id)
    finally:
        blocking_parser.release.set()
        stale_thread.join(timeout=5)

    assert not stale_thread.is_alive()
    assert reclaimed_result.state is ProcessingState.LEXICAL_READY
    assert reclaimed_result.attempt_count == 2
    assert len(stale_errors) == 1
    assert isinstance(stale_errors[0], ProcessingRunConflictError)

    session_factory = create_session_factory(integration_database_engine)
    with transactional_session(session_factory) as session:
        persisted_run = SqlAlchemyProcessingRepository(session).get_run_by_id(
            context,
            capture.processing_run_id,
        )
        chunk_set_count = session.scalar(
            select(func.count())
            .select_from(ChunkSetRow)
            .where(ChunkSetRow.processing_run_id == capture.processing_run_id)
        )

    assert persisted_run == reclaimed_result
    assert chunk_set_count == 1
