"""Unit tests for provider-neutral active-chunk indexing orchestration."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.domain import ProviderUsage
from investment_research.modules.processing.domain import Chunk, ProcessingRun, ProcessingState
from investment_research.modules.search.application import EmbeddingResponseContractError
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_BATCH_LIMIT,
    M1_EMBEDDING_DIMENSIONS,
    DenseIndexWriteRequest,
    DenseIndexWriteResult,
    DenseVectorId,
    EmbeddingRequest,
    EmbeddingResult,
    EmbeddingVector,
)
from investment_research.modules.search.indexing import (
    DenseIndexingInputError,
    DenseIndexReadinessTimeoutError,
    IndexActiveChunksService,
)
from investment_research.modules.search.ports import (
    SearchProviderContractError,
    SearchProviderTransientError,
)

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
FOREIGN_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000003")
PROCESSING_RUN_ID = UUID("10000000-0000-4000-8000-000000000001")
DOCUMENT_VERSION_ID = UUID("20000000-0000-4000-8000-000000000001")
CHUNK_SET_ID = UUID("30000000-0000-4000-8000-000000000001")
STRUCTURE_BLOCK_ID = UUID("40000000-0000-4000-8000-000000000001")
LEASE_ID = UUID("50000000-0000-4000-8000-000000000001")
CREATED_AT = datetime(2026, 9, 18, 12, tzinfo=UTC)
EMBEDDING_CONFIG_VERSION = "openai-text-embedding-3-small-1536-v1"
DENSE_INDEX_CONFIG_VERSION = "pinecone-serverless-cosine-1536-aws-us-east-1-v1"
VECTOR_VALUES = (0.125,) * M1_EMBEDDING_DIMENSIONS


def make_context(workspace_id: UUID = WORKSPACE_ID) -> WorkspaceContext:
    """Build one trusted test context."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=workspace_id)


def make_run() -> ProcessingRun:
    """Build one leased M1 run ready for provider-backed indexing."""

    return ProcessingRun(
        id=PROCESSING_RUN_ID,
        workspace_id=WORKSPACE_ID,
        document_version_id=DOCUMENT_VERSION_ID,
        state=ProcessingState.INDEXING,
        parser_version="synthetic-parser-v1",
        parser_config_version="synthetic-parser-config-v1",
        chunker_version="synthetic-chunker-v1",
        chunking_config_version="synthetic-chunking-config-v1",
        lexical_config_version="synthetic-lexical-v1",
        embedding_config_version=EMBEDDING_CONFIG_VERSION,
        dense_index_config_version=DENSE_INDEX_CONFIG_VERSION,
        created_at=CREATED_AT,
        attempt_count=1,
        lease_id=LEASE_ID,
        lease_expires_at=CREATED_AT + timedelta(minutes=6),
        started_at=CREATED_AT,
    )


def make_chunk(ordinal: int, *, text: str | None = None) -> Chunk:
    """Build one canonical chunk whose identity is stable for an ordinal."""

    return Chunk(
        id=UUID(int=ordinal + 1),
        workspace_id=WORKSPACE_ID,
        processing_run_id=PROCESSING_RUN_ID,
        document_version_id=DOCUMENT_VERSION_ID,
        chunk_set_id=CHUNK_SET_ID,
        structure_block_id=STRUCTURE_BLOCK_ID,
        ordinal=ordinal,
        text=text or f"Canonical chunk {ordinal}",
        token_count=3,
        created_at=CREATED_AT,
    )


@dataclass(slots=True)
class RecordingEmbeddingProvider:
    """Return correlated vectors while exposing controlled failure modes."""

    transient_failures: int = 0
    reverse_response: bool = False
    calls: list[EmbeddingRequest] = field(default_factory=list)

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        self.calls.append(request)
        if self.transient_failures > 0:
            self.transient_failures -= 1
            raise SearchProviderTransientError("synthetic transient embedding failure")
        vectors = tuple(
            EmbeddingVector(
                input_id=item.input_id,
                values=VECTOR_VALUES,
                embedding_config_version=request.embedding_config_version,
            )
            for item in request.inputs
        )
        return EmbeddingResult(
            vectors=tuple(reversed(vectors)) if self.reverse_response else vectors,
            usage=ProviderUsage(input_units=0, output_units=0),
        )


@dataclass(slots=True)
class RecordingDenseIndexWriter:
    """Record provider-neutral writes and return controlled visibility snapshots."""

    transient_upsert_failures: int = 0
    acknowledgement_override: tuple[DenseVectorId, ...] | None = None
    visibility_snapshots: list[tuple[DenseVectorId, ...]] = field(default_factory=list)
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
        if self.transient_upsert_failures > 0:
            self.transient_upsert_failures -= 1
            raise SearchProviderTransientError("synthetic transient upsert failure")
        vector_ids = self.acknowledgement_override or tuple(
            vector.vector_id for vector in request.vectors
        )
        return DenseIndexWriteResult(vector_ids=vector_ids)

    def fetch_visible(
        self,
        context: WorkspaceContext,
        vector_ids: Sequence[DenseVectorId],
    ) -> tuple[DenseVectorId, ...]:
        requested_ids = tuple(vector_ids)
        self.fetch_calls.append((context, requested_ids))
        if self.visibility_snapshots:
            visible = set(self.visibility_snapshots.pop(0))
            return tuple(vector_id for vector_id in requested_ids if vector_id in visible)
        return requested_ids


@dataclass(slots=True)
class FakeTime:
    """Advance monotonic time only when the service requests a wait."""

    current: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += seconds


def make_service(
    *,
    embedding_provider: RecordingEmbeddingProvider | None = None,
    dense_index_writer: RecordingDenseIndexWriter | None = None,
    fake_time: FakeTime | None = None,
    readiness_timeout_seconds: float = 30.0,
) -> tuple[
    IndexActiveChunksService,
    RecordingEmbeddingProvider,
    RecordingDenseIndexWriter,
    FakeTime,
]:
    """Build one service and return all observable collaborators."""

    provider = embedding_provider or RecordingEmbeddingProvider()
    writer = dense_index_writer or RecordingDenseIndexWriter()
    clock = fake_time or FakeTime()
    return (
        IndexActiveChunksService(
            embedding_provider=provider,
            dense_index_writer=writer,
            readiness_timeout_seconds=readiness_timeout_seconds,
            monotonic_clock=clock.monotonic,
            sleeper=clock.sleep,
        ),
        provider,
        writer,
        clock,
    )


def test_index_orders_chunks_batches_requests_and_waits_for_all_vectors() -> None:
    service, provider, writer, _ = make_service()
    chunks = tuple(make_chunk(ordinal) for ordinal in reversed(range(17)))

    result = service.index(make_context(), make_run(), chunks)

    assert result.batch_count == 2
    assert len(result.vector_ids) == 17
    assert [len(call.inputs) for call in provider.calls] == [M1_EMBEDDING_BATCH_LIMIT, 1]
    assert [item.input_id for call in provider.calls for item in call.inputs] == [
        make_chunk(ordinal).id for ordinal in range(17)
    ]
    assert [len(request.vectors) for _, request in writer.upsert_calls] == [16, 1]
    assert [len(vector_ids) for _, vector_ids in writer.fetch_calls] == [16, 1]
    first_vector = writer.upsert_calls[0][1].vectors[0]
    assert first_vector.document_version_id == DOCUMENT_VERSION_ID
    assert first_vector.chunk_set_id == CHUNK_SET_ID
    assert first_vector.embedding_config_version == EMBEDDING_CONFIG_VERSION
    assert first_vector.dense_index_config_version == DENSE_INDEX_CONFIG_VERSION


def test_index_splits_batches_at_total_character_limit() -> None:
    service, provider, writer, _ = make_service()
    chunks = (make_chunk(0, text="a" * 20_000), make_chunk(1, text="b" * 20_000))

    result = service.index(make_context(), make_run(), chunks)

    assert result.batch_count == 2
    assert [len(call.inputs) for call in provider.calls] == [1, 1]
    assert [len(request.vectors) for _, request in writer.upsert_calls] == [1, 1]


def test_index_retries_only_classified_transient_embedding_and_upsert_errors() -> None:
    provider = RecordingEmbeddingProvider(transient_failures=1)
    writer = RecordingDenseIndexWriter(transient_upsert_failures=1)
    service, _, _, fake_time = make_service(
        embedding_provider=provider,
        dense_index_writer=writer,
    )

    service.index(make_context(), make_run(), (make_chunk(0),))

    assert len(provider.calls) == 2
    assert len(writer.upsert_calls) == 2
    assert fake_time.sleeps == [0.25, 0.25]


def test_index_stops_after_two_transient_embedding_attempts() -> None:
    provider = RecordingEmbeddingProvider(transient_failures=2)
    service, _, writer, fake_time = make_service(embedding_provider=provider)

    with pytest.raises(SearchProviderTransientError, match="transient embedding"):
        service.index(make_context(), make_run(), (make_chunk(0),))

    assert len(provider.calls) == 2
    assert writer.upsert_calls == []
    assert fake_time.sleeps == [0.25]


def test_index_rejects_reordered_embedding_response_before_upsert() -> None:
    service, _, writer, _ = make_service(
        embedding_provider=RecordingEmbeddingProvider(reverse_response=True)
    )

    with pytest.raises(EmbeddingResponseContractError, match="exactly once and in order"):
        service.index(make_context(), make_run(), (make_chunk(0), make_chunk(1)))

    assert writer.upsert_calls == []


def test_index_rejects_unexpected_provider_acknowledgement() -> None:
    unexpected_id = "v1:" + "f" * 64
    service, _, _, _ = make_service(
        dense_index_writer=RecordingDenseIndexWriter(acknowledgement_override=(unexpected_id,))
    )

    with pytest.raises(SearchProviderContractError, match="unexpected vector identities"):
        service.index(make_context(), make_run(), (make_chunk(0),))


def test_index_polls_visibility_within_one_total_deadline() -> None:
    writer = RecordingDenseIndexWriter(visibility_snapshots=[(), ()])
    service, _, _, fake_time = make_service(
        dense_index_writer=writer,
        readiness_timeout_seconds=0.5,
    )

    with pytest.raises(DenseIndexReadinessTimeoutError, match="deadline"):
        service.index(make_context(), make_run(), (make_chunk(0),))

    assert len(writer.fetch_calls) == 2
    assert fake_time.sleeps == [0.5]


def test_index_completes_when_vectors_become_visible_after_one_poll() -> None:
    writer = RecordingDenseIndexWriter(visibility_snapshots=[()])
    service, _, _, fake_time = make_service(dense_index_writer=writer)

    result = service.index(make_context(), make_run(), (make_chunk(0),))

    assert len(result.vector_ids) == 1
    assert len(writer.fetch_calls) == 2
    assert fake_time.sleeps == [0.5]


def test_index_rejects_foreign_or_incoherent_canonical_chunks() -> None:
    service, provider, writer, _ = make_service()
    foreign_chunk = make_chunk(0).model_copy(update={"workspace_id": FOREIGN_WORKSPACE_ID})

    with pytest.raises(DenseIndexingInputError, match="do not belong"):
        service.index(make_context(), make_run(), (foreign_chunk,))

    assert provider.calls == []
    assert writer.upsert_calls == []
