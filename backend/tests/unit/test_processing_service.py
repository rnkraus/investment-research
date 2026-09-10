"""Unit tests for the single-document processing application service."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import BinaryIO
from uuid import UUID

import pytest

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.application import (
    ProcessDocumentService,
    ProcessingParserVersionError,
    ProcessingRunUnavailableError,
)
from investment_research.modules.processing.contracts import (
    ParsedDocument,
    ParsedPage,
    ParserLimits,
)
from investment_research.modules.processing.domain import (
    Chunk,
    ChunkSet,
    ProcessingRun,
    ProcessingState,
    StructureBlock,
)
from investment_research.modules.processing.ports import (
    DocumentParserError,
    ProcessingRepository,
    ProcessingUnitOfWork,
)

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
DOCUMENT_VERSION_ID = UUID("10000000-0000-4000-8000-000000000001")
PROCESSING_RUN_ID = UUID("20000000-0000-4000-8000-000000000001")
CHUNK_SET_ID = UUID("30000000-0000-4000-8000-000000000001")
BLOCK_ID = UUID("30000000-0000-4000-8000-000000000002")
CHUNK_ID = UUID("30000000-0000-4000-8000-000000000003")
CREATED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)
STARTED_AT = CREATED_AT + timedelta(seconds=1)
COMPLETED_AT = CREATED_AT + timedelta(seconds=2)
PARSER_LIMITS = ParserLimits(max_pages=10, max_extracted_characters=10_000)


def make_context() -> WorkspaceContext:
    """Build the trusted workspace context used by the service tests."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)


def make_run() -> ProcessingRun:
    """Build one captured run compatible with the recording parser."""

    return ProcessingRun(
        id=PROCESSING_RUN_ID,
        workspace_id=WORKSPACE_ID,
        document_version_id=DOCUMENT_VERSION_ID,
        state=ProcessingState.CAPTURED,
        parser_version="synthetic-parser-1",
        parser_config_version="parser-m0.0-v1",
        chunker_version="structure-block-1",
        chunking_config_version="chunking-m0.0-v1",
        lexical_config_version="english-lexical-v1",
        created_at=CREATED_AT,
    )


class InMemoryProcessingRepository:
    """Record service interactions while keeping one current run snapshot."""

    def __init__(self, run: ProcessingRun | None) -> None:
        self.run = run
        self.updates: list[ProcessingRun] = []
        self.lexical_outputs: list[
            tuple[tuple[StructureBlock, ...], ChunkSet, tuple[Chunk, ...]]
        ] = []

    def add_run(self, context: WorkspaceContext, run: ProcessingRun) -> None:
        raise AssertionError("processing service must not create capture runs")

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

    def add_lexical_output(
        self,
        context: WorkspaceContext,
        blocks: Sequence[StructureBlock],
        chunk_set: ChunkSet,
        chunks: Sequence[Chunk],
    ) -> None:
        assert context.workspace_id == chunk_set.workspace_id
        self.lexical_outputs.append((tuple(blocks), chunk_set, tuple(chunks)))

    def _scoped_run(
        self,
        context: WorkspaceContext,
        processing_run_id: UUID,
    ) -> ProcessingRun | None:
        if (
            self.run is None
            or self.run.workspace_id != context.workspace_id
            or self.run.id != processing_run_id
        ):
            return None
        return self.run


@dataclass(frozen=True, slots=True)
class RecordingProcessingUnitOfWork:
    """Expose the in-memory repository through the application-owned contract."""

    processing: ProcessingRepository


class RecordingUnitOfWorkFactory:
    """Count the short transactions opened by one processing attempt."""

    def __init__(self, repository: ProcessingRepository) -> None:
        self._repository = repository
        self.transaction_count = 0

    @contextmanager
    def open(self) -> Iterator[ProcessingUnitOfWork]:
        self.transaction_count += 1
        yield RecordingProcessingUnitOfWork(processing=self._repository)


class RecordingOriginalProvider:
    """Provide deterministic bytes and record the requested document version."""

    def __init__(self) -> None:
        self.calls: list[tuple[WorkspaceContext, UUID]] = []

    @contextmanager
    def open_for_processing(
        self,
        context: WorkspaceContext,
        document_version_id: UUID,
    ) -> Iterator[BinaryIO]:
        self.calls.append((context, document_version_id))
        with BytesIO(b"synthetic PDF bytes") as content:
            yield content


class RecordingParser:
    """Return a configured result or raise a configured safe parser error."""

    def __init__(
        self,
        *,
        parsed_document: ParsedDocument | None = None,
        error: DocumentParserError | None = None,
        parser_version: str = "synthetic-parser-1",
    ) -> None:
        self._parsed_document = parsed_document
        self._error = error
        self._parser_version = parser_version
        self.calls: list[ParserLimits] = []

    @property
    def parser_version(self) -> str:
        return self._parser_version

    def parse(self, content: BinaryIO, *, limits: ParserLimits) -> ParsedDocument:
        assert content.read() == b"synthetic PDF bytes"
        self.calls.append(limits)
        if self._error is not None:
            raise self._error
        assert self._parsed_document is not None
        return self._parsed_document


def deterministic_ids() -> Iterator[UUID]:
    """Yield identifiers for one chunk set, structure block, and chunk."""

    yield from (CHUNK_SET_ID, BLOCK_ID, CHUNK_ID)


def deterministic_clock() -> Iterator[datetime]:
    """Yield the start and terminal timestamps used by one attempt."""

    yield from (STARTED_AT, COMPLETED_AT)


def make_service(
    repository: InMemoryProcessingRepository,
    parser: RecordingParser,
    original_provider: RecordingOriginalProvider,
) -> tuple[ProcessDocumentService, RecordingUnitOfWorkFactory]:
    """Build one deterministic service and expose its transaction recorder."""

    unit_of_work_factory = RecordingUnitOfWorkFactory(repository)
    timestamps = deterministic_clock()
    identifiers = deterministic_ids()
    return (
        ProcessDocumentService(
            parser=parser,
            original_provider=original_provider,
            unit_of_work_factory=unit_of_work_factory.open,
            parser_limits=PARSER_LIMITS,
            clock=lambda: next(timestamps),
            identifier_factory=lambda: next(identifiers),
        ),
        unit_of_work_factory,
    )


def test_process_commits_start_and_lexical_output_in_separate_transactions() -> None:
    context = make_context()
    repository = InMemoryProcessingRepository(make_run())
    parsed_document = ParsedDocument(
        pages=(ParsedPage(page_number=1, text="Revenue grew by 22%."),)
    )
    parser = RecordingParser(parsed_document=parsed_document)
    original_provider = RecordingOriginalProvider()
    service, unit_of_work_factory = make_service(repository, parser, original_provider)

    result = service.process(context, PROCESSING_RUN_ID)

    assert result.state is ProcessingState.LEXICAL_READY
    assert result.started_at == STARTED_AT
    assert result.completed_at == COMPLETED_AT
    assert repository.run == result
    assert [run.state for run in repository.updates] == [
        ProcessingState.PARSING,
        ProcessingState.LEXICAL_READY,
    ]
    assert unit_of_work_factory.transaction_count == 2
    assert original_provider.calls == [(context, DOCUMENT_VERSION_ID)]
    assert parser.calls == [PARSER_LIMITS]
    blocks, chunk_set, chunks = repository.lexical_outputs[0]
    assert tuple(block.id for block in blocks) == (BLOCK_ID,)
    assert chunk_set.id == CHUNK_SET_ID
    assert tuple(chunk.id for chunk in chunks) == (CHUNK_ID,)


def test_parser_failure_is_recorded_safely_in_a_new_transaction() -> None:
    repository = InMemoryProcessingRepository(make_run())
    parser_error = DocumentParserError("sensitive parser detail must not be persisted")
    parser = RecordingParser(error=parser_error)
    service, unit_of_work_factory = make_service(
        repository,
        parser,
        RecordingOriginalProvider(),
    )

    with pytest.raises(DocumentParserError) as raised:
        service.process(make_context(), PROCESSING_RUN_ID)

    assert raised.value is parser_error
    assert repository.run is not None
    assert repository.run.state is ProcessingState.FAILED
    assert repository.run.error_summary == "Document parsing failed safely"
    assert "sensitive" not in repository.run.error_summary
    assert [run.state for run in repository.updates] == [
        ProcessingState.PARSING,
        ProcessingState.FAILED,
    ]
    assert repository.lexical_outputs == []
    assert unit_of_work_factory.transaction_count == 2


def test_process_hides_unknown_and_cross_workspace_run_identifiers() -> None:
    repository = InMemoryProcessingRepository(None)
    parser = RecordingParser(
        parsed_document=ParsedDocument(pages=(ParsedPage(page_number=1, text="Text"),))
    )
    original_provider = RecordingOriginalProvider()
    service, unit_of_work_factory = make_service(repository, parser, original_provider)

    with pytest.raises(ProcessingRunUnavailableError, match="unavailable"):
        service.process(make_context(), PROCESSING_RUN_ID)

    assert parser.calls == []
    assert original_provider.calls == []
    assert unit_of_work_factory.transaction_count == 1


def test_parser_version_mismatch_fails_before_original_access() -> None:
    repository = InMemoryProcessingRepository(make_run())
    parser = RecordingParser(
        parsed_document=ParsedDocument(pages=(ParsedPage(page_number=1, text="Text"),)),
        parser_version="synthetic-parser-2",
    )
    original_provider = RecordingOriginalProvider()
    service, unit_of_work_factory = make_service(repository, parser, original_provider)

    with pytest.raises(ProcessingParserVersionError, match="does not match"):
        service.process(make_context(), PROCESSING_RUN_ID)

    assert repository.run is not None
    assert repository.run.state is ProcessingState.FAILED
    assert repository.run.error_summary == "Configured parser version is unavailable"
    assert parser.calls == []
    assert original_provider.calls == []
    assert unit_of_work_factory.transaction_count == 2
