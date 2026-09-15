"""PostgreSQL integration tests for the single-document processing service."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
    ProcessDocumentService,
    ProcessingRunUnavailableError,
)
from investment_research.modules.processing.contracts import ParserLimits
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.processing.persistence import (
    ChunkRow,
    ChunkSetRow,
    ProcessingRunRow,
    SqlAlchemyProcessingRepository,
    StructureBlockRow,
)
from investment_research.modules.processing.ports import ParserLimitExceededError
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
) -> ProcessDocumentService:
    """Wire real M0.0 adapters around the processing application service."""

    session_factory = create_session_factory(engine)
    timestamps = processing_clock()
    return ProcessDocumentService(
        parser=PypdfDocumentParser(),
        original_provider=OpenOriginalService(
            object_storage=storage,
            unit_of_work_factory=create_document_read_unit_of_work_factory(session_factory),
        ),
        unit_of_work_factory=create_processing_unit_of_work_factory(session_factory),
        parser_limits=limits,
        clock=lambda: next(timestamps),
    )


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
