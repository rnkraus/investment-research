"""PostgreSQL integration tests for the single-document processing service."""

import socket
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from typing import BinaryIO, cast
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
from investment_research.infrastructure.parsing.client import HttpDocumentParser
from investment_research.infrastructure.parsing.http_protocol import PARSER_RESPONSE_CONTENT_TYPE
from investment_research.infrastructure.parsing.media import MediaAwareDocumentParser
from investment_research.infrastructure.parsing.pypdf import PypdfDocumentParser
from investment_research.modules.documents.application import (
    CaptureDocumentService,
    OpenOriginalService,
)
from investment_research.modules.documents.contracts import (
    CaptureDocumentResult,
    PastedTextCaptureInput,
    PdfCaptureInput,
)
from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.documents.persistence import SqlAlchemyDocumentRepository
from investment_research.modules.identity.domain import (
    Workspace,
    WorkspaceContext,
    WorkspaceType,
)
from investment_research.modules.identity.persistence import SqlAlchemyWorkspaceRepository
from investment_research.modules.processing.application import (
    M0_0_PROCESSING_PROFILE,
    M1_PROCESSING_PROFILE,
    ExecuteProcessingJobService,
    ProcessDocumentService,
    ProcessingProfile,
    ProcessingRetryableError,
    ProcessingRunAlreadyActiveError,
    ProcessingRunConflictError,
    ProcessingRunUnavailableError,
)
from investment_research.modules.processing.contracts import (
    PDF_PARSER_CONFIG_VERSION_V1,
    ParsedDocument,
    ParserLimits,
    ParserMediaType,
    ProcessingJobV1,
)
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.processing.indexing import IndexProcessingRunService
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
    ParserProtocolError,
    ParserTransportError,
)
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_DIMENSIONS,
    DenseIndexWriteRequest,
    DenseIndexWriteResult,
    DenseVectorId,
    EmbeddingRequest,
    EmbeddingVector,
)
from investment_research.modules.search.indexing import IndexActiveChunksService
from tests.fakes import RecordingProcessingDispatcher

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_PDF = (
    REPOSITORY_ROOT / "fixtures/m0_0/documents/northstar-grid-analytics-fy2025-earnings-brief.pdf"
)
CAPTURED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)
STARTED_AT = CAPTURED_AT + timedelta(seconds=1)
COMPLETED_AT = CAPTURED_AT + timedelta(seconds=2)
INDEXING_STARTED_AT = CAPTURED_AT + timedelta(seconds=3)
INDEXING_COMPLETED_AT = CAPTURED_AT + timedelta(seconds=4)
VECTOR_VALUES = (0.125,) * M1_EMBEDDING_DIMENSIONS


@dataclass(frozen=True, slots=True)
class StaticIdentityContextProvider:
    """Return one integration-test context from server-controlled state."""

    context: WorkspaceContext

    def resolve(self) -> WorkspaceContext:
        return self.context


@dataclass(slots=True)
class RecordingEmbeddingProvider:
    """Return one fixed valid vector for every canonical document chunk."""

    calls: list[EmbeddingRequest] = field(default_factory=list)

    def embed(self, request: EmbeddingRequest) -> tuple[EmbeddingVector, ...]:
        self.calls.append(request)
        return tuple(
            EmbeddingVector(
                input_id=item.input_id,
                values=VECTOR_VALUES,
                embedding_config_version=request.embedding_config_version,
            )
            for item in request.inputs
        )


@dataclass(slots=True)
class ImmediatelyVisibleDenseIndexWriter:
    """Record deterministic writes and make acknowledged IDs immediately visible."""

    upsert_calls: list[tuple[WorkspaceContext, DenseIndexWriteRequest]] = field(
        default_factory=list
    )
    fetch_calls: list[tuple[WorkspaceContext, tuple[DenseVectorId, ...]]] = field(
        default_factory=list
    )

    def upsert(
        self,
        context: WorkspaceContext,
        request: DenseIndexWriteRequest,
    ) -> DenseIndexWriteResult:
        self.upsert_calls.append((context, request))
        return DenseIndexWriteResult(
            vector_ids=tuple(vector.vector_id for vector in request.vectors)
        )

    def fetch_visible(
        self,
        context: WorkspaceContext,
        vector_ids: Sequence[DenseVectorId],
    ) -> tuple[DenseVectorId, ...]:
        visible_ids = tuple(vector_ids)
        self.fetch_calls.append((context, visible_ids))
        return visible_ids


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
    *,
    profile: ProcessingProfile = M0_0_PROCESSING_PROFILE,
) -> CaptureDocumentResult:
    """Capture the public fixture through the same service used by the API."""

    session_factory = create_session_factory(engine)
    service = CaptureDocumentService(
        object_storage=storage,
        unit_of_work_factory=create_capture_unit_of_work_factory(session_factory),
        max_upload_bytes=FIXTURE_PDF.stat().st_size + 1,
        processing_dispatcher=RecordingProcessingDispatcher(),
        processing_profile=profile,
        clock=lambda: CAPTURED_AT,
    )
    capture_input = PdfCaptureInput(
        filename=FIXTURE_PDF.name,
        media_type="application/pdf",
        title="Northstar Grid Analytics FY2025 Earnings Brief",
        language="en",
        published_at=CAPTURED_AT,
        available_at=CAPTURED_AT,
    )
    with FIXTURE_PDF.open("rb") as content:
        result = service.capture_pdf(context, capture_input, content)
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

    def parse(
        self,
        content: BinaryIO,
        *,
        limits: ParserLimits,
        media_type: ParserMediaType = "application/pdf",
    ) -> ParsedDocument:
        """Wait for the test before parsing the caller-owned original stream."""

        self.entered.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("blocking parser was not released by the test")
        return self._delegate.parse(content, limits=limits, media_type=media_type)


class FailOnceParser:
    """Simulate one transient parser transport failure before recovery."""

    def __init__(self) -> None:
        self._delegate = PypdfDocumentParser()
        self.calls = 0

    @property
    def parser_version(self) -> str:
        """Expose the same reproducible version as the real parser."""

        return self._delegate.parser_version

    def parse(
        self,
        content: BinaryIO,
        *,
        limits: ParserLimits,
        media_type: ParserMediaType = "application/pdf",
    ) -> ParsedDocument:
        """Fail the first delivery and parse normally on the retry."""

        self.calls += 1
        if self.calls == 1:
            raise ParserTransportError("synthetic private transport failure")
        return self._delegate.parse(content, limits=limits, media_type=media_type)


class ParserFailureHttpServer(ThreadingHTTPServer):
    """Expose one deterministic parser transport or protocol failure."""

    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        failure_mode: str,
    ) -> None:
        super().__init__(server_address, ParserFailureRequestHandler)
        self.failure_mode = failure_mode


class ParserFailureRequestHandler(BaseHTTPRequestHandler):
    """Return only the failure behavior selected by the integration test."""

    def do_POST(self) -> None:
        """Consume the bounded request before failing in one controlled way."""

        content_length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(content_length)
        self.close_connection = True
        server = cast(ParserFailureHttpServer, self.server)
        if server.failure_mode == "timeout":
            time.sleep(1.25)
            return
        if server.failure_mode == "disconnect":
            with suppress(OSError):
                self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if server.failure_mode != "malformed":
            raise RuntimeError("unsupported parser failure mode")

        body = b"not-json"
        self.send_response(200)
        self.send_header("Content-Type", PARSER_RESPONSE_CONTENT_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, message_format: str, *args: object) -> None:
        """Keep deterministic integration-test output free of server logs."""

        del message_format, args


@contextmanager
def parser_failure_endpoint(failure_mode: str) -> Iterator[str]:
    """Serve one local failure endpoint and always stop its listener thread."""

    server = ParserFailureHttpServer(("127.0.0.1", 0), failure_mode)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


def make_http_parser(endpoint_url: str) -> HttpDocumentParser:
    """Create the real worker-side HTTP adapter with a short test timeout."""

    return HttpDocumentParser(
        endpoint_url=endpoint_url,
        parser_version=PypdfDocumentParser().parser_version,
        parser_config_version=PDF_PARSER_CONFIG_VERSION_V1,
        timeout_seconds=1,
        max_request_bytes=FIXTURE_PDF.stat().st_size + 1,
    )


def assert_safe_parser_failure(
    engine: Engine,
    context: WorkspaceContext,
    storage: LocalObjectStorage,
    capture: CaptureDocumentResult,
    *,
    expected_attempt_count: int,
    expected_summary: str,
) -> None:
    """Require one inspectable failure, intact original, and no derived rows."""

    session_factory = create_session_factory(engine)
    with transactional_session(session_factory) as session:
        processing = SqlAlchemyProcessingRepository(session)
        documents = SqlAlchemyDocumentRepository(session)
        persisted_run = processing.get_run_by_id(context, capture.processing_run_id)
        original = documents.get_original_artifact(
            context,
            capture.document_id,
            capture.document_version_id,
        )
        block_count = session.scalar(
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
    assert persisted_run.attempt_count == expected_attempt_count
    assert persisted_run.lease_id is None
    assert persisted_run.lease_expires_at is None
    assert persisted_run.error_summary == expected_summary
    assert original is not None
    with storage.open(context, original.storage_key) as content:
        assert content.read() == FIXTURE_PDF.read_bytes()
    assert (block_count, chunk_set_count, chunk_count) == (0, 0, 0)


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


def test_m1_job_reaches_searchable_and_duplicate_delivery_is_a_noop(
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    context = create_workspace(integration_database_engine)
    storage = LocalObjectStorage(tmp_path / "objects")
    capture = capture_fixture(
        integration_database_engine,
        context,
        storage,
        profile=M1_PROCESSING_PROFILE,
    )
    processing_service = make_processing_service(
        integration_database_engine,
        storage,
        limits=ParserLimits(max_pages=10, max_extracted_characters=100_000),
        profile=M1_PROCESSING_PROFILE,
    )
    embedding_provider = RecordingEmbeddingProvider()
    dense_index_writer = ImmediatelyVisibleDenseIndexWriter()
    session_factory = create_session_factory(integration_database_engine)
    indexing_timestamps = iter((INDEXING_STARTED_AT, INDEXING_COMPLETED_AT))
    indexing_service = IndexProcessingRunService(
        indexing_service=IndexActiveChunksService(
            embedding_provider=embedding_provider,
            dense_index_writer=dense_index_writer,
        ),
        unit_of_work_factory=create_processing_unit_of_work_factory(session_factory),
        profile=M1_PROCESSING_PROFILE,
        clock=lambda: next(indexing_timestamps),
    )
    job_service = ExecuteProcessingJobService(
        processing_service=processing_service,
        indexing_service=indexing_service,
        identity_context_provider=StaticIdentityContextProvider(context),
    )
    job = ProcessingJobV1.create(
        workspace_id=context.workspace_id,
        processing_run_id=capture.processing_run_id,
    )

    first_result = job_service.execute(job)
    duplicate_result = job_service.execute(job)

    assert first_result.state is ProcessingState.SEARCHABLE
    assert first_result.started_at == STARTED_AT
    assert first_result.completed_at == INDEXING_COMPLETED_AT
    assert first_result.attempt_count == 1
    assert first_result.lease_id is None
    assert duplicate_result == first_result
    assert len(embedding_provider.calls) == 1
    assert len(dense_index_writer.upsert_calls) == 1
    assert len(dense_index_writer.fetch_calls) == 1
    written_vectors = dense_index_writer.upsert_calls[0][1].vectors
    assert len(written_vectors) == 4
    assert len({vector.vector_id for vector in written_vectors}) == 4

    with transactional_session(session_factory) as session:
        persisted_run = SqlAlchemyProcessingRepository(session).get_run_by_id(
            context,
            capture.processing_run_id,
        )

    assert persisted_run == first_result


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


@pytest.mark.parametrize("failure_mode", ["timeout", "disconnect"])
def test_http_transport_failure_retries_then_records_safe_canonical_state(
    integration_database_engine: Engine,
    tmp_path: Path,
    failure_mode: str,
) -> None:
    context = create_workspace(integration_database_engine)
    storage = LocalObjectStorage(tmp_path / "objects")
    capture = capture_fixture(integration_database_engine, context, storage)
    timestamps = iter(
        CAPTURED_AT + timedelta(seconds=offset)
        for offset in range(1, M0_0_PROCESSING_PROFILE.maximum_attempts * 2 + 1)
    )

    with parser_failure_endpoint(failure_mode) as endpoint_url:
        service = make_processing_service(
            integration_database_engine,
            storage,
            limits=ParserLimits(max_pages=10, max_extracted_characters=100_000),
            parser=make_http_parser(endpoint_url),
            timestamps=timestamps,
        )
        for _ in range(M0_0_PROCESSING_PROFILE.maximum_attempts - 1):
            with pytest.raises(
                ProcessingRetryableError,
                match="processing dependency is temporarily unavailable",
            ):
                service.process(context, capture.processing_run_id)
        with pytest.raises(ParserTransportError, match="isolated parser transport failed"):
            service.process(context, capture.processing_run_id)

    assert_safe_parser_failure(
        integration_database_engine,
        context,
        storage,
        capture,
        expected_attempt_count=M0_0_PROCESSING_PROFILE.maximum_attempts,
        expected_summary="Processing attempt limit exceeded",
    )


def test_malformed_http_parser_response_fails_once_without_partial_output(
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    context = create_workspace(integration_database_engine)
    storage = LocalObjectStorage(tmp_path / "objects")
    capture = capture_fixture(integration_database_engine, context, storage)

    with parser_failure_endpoint("malformed") as endpoint_url:
        service = make_processing_service(
            integration_database_engine,
            storage,
            limits=ParserLimits(max_pages=10, max_extracted_characters=100_000),
            parser=make_http_parser(endpoint_url),
        )
        with pytest.raises(ParserProtocolError, match="parser response is invalid"):
            service.process(context, capture.processing_run_id)

    assert_safe_parser_failure(
        integration_database_engine,
        context,
        storage,
        capture,
        expected_attempt_count=1,
        expected_summary="Document parsing failed safely",
    )


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


def test_m1_pasted_text_reaches_lexical_ready_with_exact_persisted_ranges(
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    context = create_workspace(integration_database_engine)
    storage = LocalObjectStorage(tmp_path / "objects")
    session_factory = create_session_factory(integration_database_engine)
    capture_service = CaptureDocumentService(
        object_storage=storage,
        unit_of_work_factory=create_capture_unit_of_work_factory(session_factory),
        max_upload_bytes=1_024,
        processing_dispatcher=RecordingProcessingDispatcher(),
        processing_profile=M1_PROCESSING_PROFILE,
        clock=lambda: CAPTURED_AT,
    )
    source = "  Revenue grew.  \n\nMargin expanded.\n"
    capture = capture_service.capture_text(
        context,
        PastedTextCaptureInput(
            title="Synthetic pasted research",
            language="en",
            published_at=CAPTURED_AT,
            available_at=CAPTURED_AT,
            text=source,
        ),
    )
    service = make_processing_service(
        integration_database_engine,
        storage,
        limits=ParserLimits(max_pages=10, max_extracted_characters=1_000),
        parser=MediaAwareDocumentParser(),
        profile=M1_PROCESSING_PROFILE,
    )

    result = service.process(context, capture.processing_run_id)

    with transactional_session(session_factory) as session:
        persisted_blocks = tuple(
            (
                row.source_locator_kind,
                row.page_number,
                row.text,
                row.start_character,
                row.end_character,
            )
            for row in session.scalars(
                select(StructureBlockRow)
                .where(StructureBlockRow.processing_run_id == capture.processing_run_id)
                .order_by(StructureBlockRow.ordinal)
            )
        )
    assert result.state is ProcessingState.LEXICAL_READY
    assert tuple(block[0] for block in persisted_blocks) == (
        SourceLocatorKind.TEXT_PASSAGE,
        SourceLocatorKind.TEXT_PASSAGE,
    )
    assert tuple(block[1] for block in persisted_blocks) == (None, None)
    assert tuple(block[2] for block in persisted_blocks) == (
        "Revenue grew.",
        "Margin expanded.",
    )
    for _, _, text, start_character, end_character in persisted_blocks:
        assert source[start_character:end_character] == text


def test_m1_pdf_reaches_lexical_ready_with_exact_persisted_page_ranges(
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    context = create_workspace(integration_database_engine)
    storage = LocalObjectStorage(tmp_path / "objects")
    capture = capture_fixture(
        integration_database_engine,
        context,
        storage,
        profile=M1_PROCESSING_PROFILE,
    )
    limits = ParserLimits(max_pages=10, max_extracted_characters=100_000)
    service = make_processing_service(
        integration_database_engine,
        storage,
        limits=limits,
        parser=MediaAwareDocumentParser(),
        profile=M1_PROCESSING_PROFILE,
    )

    result = service.process(context, capture.processing_run_id)

    session_factory = create_session_factory(integration_database_engine)
    with transactional_session(session_factory) as session:
        persisted_blocks = tuple(
            (
                row.source_locator_kind,
                row.page_number,
                row.text,
                row.start_character,
                row.end_character,
            )
            for row in session.scalars(
                select(StructureBlockRow)
                .where(StructureBlockRow.processing_run_id == capture.processing_run_id)
                .order_by(StructureBlockRow.ordinal)
            )
        )

    with FIXTURE_PDF.open("rb") as content:
        parsed_pages = PypdfDocumentParser().parse(content, limits=limits)
    page_text_by_number = {page.page_number: page.text for page in parsed_pages.pages}

    assert result.state is ProcessingState.LEXICAL_READY
    assert result.parser_config_version == M1_PROCESSING_PROFILE.parser_config_version
    assert {block[1] for block in persisted_blocks} == {1, 2, 3, 4}
    for locator_kind, page_number, text, start_character, end_character in persisted_blocks:
        assert locator_kind is SourceLocatorKind.PDF_PAGE
        assert page_number is not None
        assert page_text_by_number[page_number][start_character:end_character] == text
