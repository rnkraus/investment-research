"""Deterministic Step 2 gate for both approved M1 document media types."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from itertools import count
from pathlib import Path
from typing import cast
from unittest.mock import Mock, create_autospec
from uuid import UUID

import pytest

from investment_research.infrastructure.parsing.media import MediaAwareDocumentParser
from investment_research.infrastructure.search.deterministic import (
    DeterministicEmbeddingCase,
    DeterministicEmbeddingProvider,
)
from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.application import (
    ExecuteProviderOperationService,
    ProviderCostPolicy,
)
from investment_research.modules.processing.application import (
    M1_PROCESSING_PROFILE,
    ProcessDocumentService,
)
from investment_research.modules.processing.contracts import ParserLimits, ParserMediaType
from investment_research.modules.processing.domain import (
    Chunk,
    ChunkSet,
    ProcessingRun,
    ProcessingState,
    StructureBlock,
)
from investment_research.modules.processing.indexing import IndexProcessingRunService
from investment_research.modules.processing.ports import (
    ProcessingOriginal,
    ProcessingRepository,
    ProcessingUnitOfWork,
)
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_DIMENSIONS,
    DenseIndexVector,
    DenseIndexWriteRequest,
    DenseIndexWriteResult,
    DenseVectorId,
    EmbeddingPurpose,
)
from investment_research.modules.search.indexing import (
    DocumentEmbeddingAccountingProfile,
    IndexActiveChunksService,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PDF_FIXTURE = (
    REPOSITORY_ROOT / "fixtures/m0_0/documents/northstar-grid-analytics-fy2025-earnings-brief.pdf"
)
TEXT_FIXTURE = (
    REPOSITORY_ROOT / "fixtures/m1/documents/northstar-grid-analytics-fy2026-capacity-update.txt"
)
USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
PROCESSING_RUN_ID = UUID("10000000-0000-4000-8000-000000000001")
DOCUMENT_VERSION_ID = UUID("20000000-0000-4000-8000-000000000001")
LEASE_ID = UUID("30000000-0000-4000-8000-000000000001")
CREATED_AT = datetime(2026, 9, 18, 12, tzinfo=UTC)
PARSER_LIMITS = ParserLimits(max_pages=10, max_extracted_characters=20_000)
VECTOR_VALUES = (0.125,) * M1_EMBEDDING_DIMENSIONS


def make_provider_operation_executor() -> Mock:
    """Run deterministic callbacks without adding persistence to the in-memory gate."""

    executor = cast(Mock, create_autospec(ExecuteProviderOperationService, instance=True))
    executor.execute.side_effect = (
        lambda _context, _attempt, provider_call, _usage_from_result, _classify_error: (
            provider_call()
        )
    )
    return executor


@dataclass(frozen=True, slots=True)
class FixtureCase:
    """Describe one approved source and its required locator kind."""

    path: Path
    media_type: ParserMediaType
    locator_kind: SourceLocatorKind
    content_sha256: str


@dataclass(slots=True)
class InMemoryProcessingRepository:
    """Retain one canonical run and its active lexical output across both services."""

    run: ProcessingRun
    updates: list[ProcessingRun] = field(default_factory=list)
    blocks: tuple[StructureBlock, ...] = ()
    chunk_set: ChunkSet | None = None
    chunks: tuple[Chunk, ...] = ()

    def add_run(self, context: WorkspaceContext, run: ProcessingRun) -> None:
        raise AssertionError("the gate starts from an existing captured run")

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
        if self.chunk_set is None or not self.chunk_set.is_active:
            return ()
        return tuple(sorted(self.chunks, key=lambda chunk: (chunk.ordinal, chunk.id)))

    def add_lexical_output(
        self,
        context: WorkspaceContext,
        blocks: Sequence[StructureBlock],
        chunk_set: ChunkSet,
        chunks: Sequence[Chunk],
    ) -> None:
        assert context.workspace_id == chunk_set.workspace_id
        self.blocks = tuple(blocks)
        self.chunk_set = chunk_set
        self.chunks = tuple(chunks)

    def _scoped_run(
        self,
        context: WorkspaceContext,
        processing_run_id: UUID,
    ) -> ProcessingRun | None:
        if context.workspace_id != self.run.workspace_id or processing_run_id != self.run.id:
            return None
        return self.run


@dataclass(frozen=True, slots=True)
class InMemoryProcessingUnitOfWork:
    """Expose the shared processing repository through the application port."""

    processing: ProcessingRepository


@dataclass(slots=True)
class InMemoryProcessingUnitOfWorkFactory:
    """Open short deterministic transactions around the shared repository."""

    repository: ProcessingRepository

    @contextmanager
    def open(self) -> Iterator[ProcessingUnitOfWork]:
        yield InMemoryProcessingUnitOfWork(processing=self.repository)


@dataclass(frozen=True, slots=True)
class FixtureOriginalProvider:
    """Open one approved immutable fixture through the processing port."""

    content: bytes
    media_type: ParserMediaType

    @contextmanager
    def open_for_processing(
        self,
        context: WorkspaceContext,
        document_version_id: UUID,
    ) -> Iterator[ProcessingOriginal]:
        assert context.workspace_id == WORKSPACE_ID
        assert document_version_id == DOCUMENT_VERSION_ID
        with BytesIO(self.content) as stream:
            yield ProcessingOriginal(content=stream, media_type=self.media_type)


@dataclass(slots=True)
class InMemoryDenseIndexWriter:
    """Apply idempotent vector writes and expose immediate deterministic visibility."""

    vectors_by_id: dict[DenseVectorId, DenseIndexVector] = field(default_factory=dict)
    upsert_history: list[tuple[DenseVectorId, ...]] = field(default_factory=list)

    def upsert(
        self,
        context: WorkspaceContext,
        request: DenseIndexWriteRequest,
    ) -> DenseIndexWriteResult:
        assert context.workspace_id == WORKSPACE_ID
        vector_ids = tuple(vector.vector_id for vector in request.vectors)
        for vector in request.vectors:
            self.vectors_by_id[vector.vector_id] = vector
        self.upsert_history.append(vector_ids)
        return DenseIndexWriteResult(vector_ids=vector_ids)

    def fetch_visible(
        self,
        context: WorkspaceContext,
        vector_ids: Sequence[DenseVectorId],
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[DenseVectorId, ...]:
        assert context.workspace_id == WORKSPACE_ID
        assert timeout_seconds is None or timeout_seconds > 0
        return tuple(vector_id for vector_id in vector_ids if vector_id in self.vectors_by_id)


def make_captured_run() -> ProcessingRun:
    """Build one captured run carrying the frozen M1 processing configuration."""

    return ProcessingRun(
        id=PROCESSING_RUN_ID,
        workspace_id=WORKSPACE_ID,
        document_version_id=DOCUMENT_VERSION_ID,
        state=ProcessingState.CAPTURED,
        parser_version=M1_PROCESSING_PROFILE.parser_version,
        parser_config_version=M1_PROCESSING_PROFILE.parser_config_version,
        chunker_version=M1_PROCESSING_PROFILE.chunker_version,
        chunking_config_version=M1_PROCESSING_PROFILE.chunking_config_version,
        lexical_config_version=M1_PROCESSING_PROFILE.lexical_config_version,
        embedding_config_version=M1_PROCESSING_PROFILE.embedding_config_version,
        dense_index_config_version=M1_PROCESSING_PROFILE.dense_index_config_version,
        created_at=CREATED_AT,
    )


@pytest.mark.parametrize(
    "fixture",
    [
        pytest.param(
            FixtureCase(
                PDF_FIXTURE,
                "application/pdf",
                SourceLocatorKind.PDF_PAGE,
                "ecb709d40762dc85febc1f6be3a7d067fc8383788ceab072ac049b3e5cfaede1",
            ),
            id="pdf",
        ),
        pytest.param(
            FixtureCase(
                TEXT_FIXTURE,
                "text/plain",
                SourceLocatorKind.TEXT_PASSAGE,
                "45985d7e98d74a7690513624914b49c6d9180842097f837798ce3f42803639b5",
            ),
            id="pasted-text",
        ),
    ],
)
def test_approved_fixture_reaches_searchable_with_idempotent_vectors(
    fixture: FixtureCase,
) -> None:
    context = WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)
    repository = InMemoryProcessingRepository(make_captured_run())
    unit_of_work_factory = InMemoryProcessingUnitOfWorkFactory(repository)
    timestamps = iter(CREATED_AT + timedelta(seconds=offset) for offset in range(1, 5))
    identifiers = count(1_000)
    fixture_content = fixture.path.read_bytes()
    assert sha256(fixture_content).hexdigest() == fixture.content_sha256
    processing_service = ProcessDocumentService(
        parser=MediaAwareDocumentParser(),
        original_provider=FixtureOriginalProvider(
            content=fixture_content,
            media_type=fixture.media_type,
        ),
        unit_of_work_factory=unit_of_work_factory.open,
        parser_limits=PARSER_LIMITS,
        profile=M1_PROCESSING_PROFILE,
        clock=lambda: next(timestamps),
        identifier_factory=lambda: UUID(int=next(identifiers)),
        lease_identifier_factory=lambda: LEASE_ID,
    )

    processing_attempt = processing_service.process_attempt(context, PROCESSING_RUN_ID)

    assert processing_attempt.run.state is ProcessingState.LEXICAL_READY
    assert processing_attempt.owned_indexing_lease_id == LEASE_ID
    assert repository.blocks
    assert repository.chunks
    assert all(block.source_locator_kind is fixture.locator_kind for block in repository.blocks)
    assert all(
        (block.page_number is not None) is (fixture.locator_kind is SourceLocatorKind.PDF_PAGE)
        for block in repository.blocks
    )

    embedding_cases = {
        chunk.text: DeterministicEmbeddingCase(
            purpose=EmbeddingPurpose.DOCUMENT,
            text=chunk.text,
            values=VECTOR_VALUES,
        )
        for chunk in repository.chunks
    }
    embedding_provider = DeterministicEmbeddingProvider(
        embedding_config_version=M1_PROCESSING_PROFILE.embedding_config_version,
        cases=tuple(embedding_cases.values()),
    )
    dense_index_writer = InMemoryDenseIndexWriter()
    indexing_service = IndexActiveChunksService(
        embedding_provider=embedding_provider,
        dense_index_writer=dense_index_writer,
        provider_operation_executor=cast(
            ExecuteProviderOperationService,
            make_provider_operation_executor(),
        ),
        embedding_accounting=DocumentEmbeddingAccountingProfile(
            provider="openai",
            model="text-embedding-3-small",
            cost_policy=ProviderCostPolicy(
                input_micro_usd_per_million_units=20_000,
                output_micro_usd_per_million_units=0,
            ),
        ),
    )
    lifecycle_service = IndexProcessingRunService(
        indexing_service=indexing_service,
        unit_of_work_factory=unit_of_work_factory.open,
        profile=M1_PROCESSING_PROFILE,
        clock=lambda: next(timestamps),
    )

    searchable_run = lifecycle_service.index(
        context,
        PROCESSING_RUN_ID,
        owned_lease_id=processing_attempt.owned_indexing_lease_id,
    )

    assert searchable_run.state is ProcessingState.SEARCHABLE
    assert searchable_run.completed_at is not None
    assert searchable_run.lease_id is None
    first_vector_ids = tuple(dense_index_writer.vectors_by_id)
    assert len(first_vector_ids) == len(repository.chunks)

    indexing_run = next(run for run in repository.updates if run.state is ProcessingState.INDEXING)
    redelivered_result = indexing_service.index(
        context,
        indexing_run,
        repository.chunks,
    )

    assert redelivered_result.vector_ids == first_vector_ids
    assert len(dense_index_writer.vectors_by_id) == len(first_vector_ids)
    writes_before_terminal_redelivery = len(dense_index_writer.upsert_history)
    assert lifecycle_service.index(context, PROCESSING_RUN_ID) == searchable_run
    assert len(dense_index_writer.upsert_history) == writes_before_terminal_redelivery
