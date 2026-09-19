"""Application-owned orchestration for bounded document-vector indexing."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from time import monotonic, sleep

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.domain import Chunk, ProcessingRun, ProcessingState
from investment_research.modules.search.application import (
    build_dense_index_vector,
    validate_embedding_response,
)
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_BATCH_LIMIT,
    M1_EMBEDDING_CHARACTER_LIMIT,
    M1_PROVIDER_MAX_ATTEMPTS,
    M1_PROVIDER_RETRY_DELAY_SECONDS,
    DenseIndexWriteRequest,
    DenseIndexWriteResult,
    DenseVectorId,
    EmbeddingInput,
    EmbeddingPurpose,
    EmbeddingRequest,
    EmbeddingResult,
)
from investment_research.modules.search.ports import (
    DenseIndexWriter,
    EmbeddingProvider,
    SearchProviderContractError,
    SearchProviderTransientError,
)

M1_DENSE_INDEX_READINESS_TIMEOUT_SECONDS = 30.0
M1_DENSE_INDEX_POLL_INTERVAL_SECONDS = 0.5

type MonotonicClock = Callable[[], float]
type Sleeper = Callable[[float], None]


class DenseIndexingInputError(ValueError):
    """Reject canonical processing data that cannot form one coherent index operation."""


class DenseIndexReadinessTimeoutError(SearchProviderTransientError):
    """Request a later retry when written vectors do not become visible in time."""


@dataclass(frozen=True, slots=True)
class DocumentIndexingResult:
    """Describe one completed document-index operation without provider response objects."""

    vector_ids: tuple[DenseVectorId, ...]
    batch_count: int


@dataclass(frozen=True, slots=True)
class IndexActiveChunksService:
    """Embed and index one run's active canonical chunks outside database transactions."""

    embedding_provider: EmbeddingProvider
    dense_index_writer: DenseIndexWriter
    readiness_timeout_seconds: float = M1_DENSE_INDEX_READINESS_TIMEOUT_SECONDS
    poll_interval_seconds: float = M1_DENSE_INDEX_POLL_INTERVAL_SECONDS
    retry_delay_seconds: float = M1_PROVIDER_RETRY_DELAY_SECONDS
    monotonic_clock: MonotonicClock = monotonic
    sleeper: Sleeper = sleep

    def __post_init__(self) -> None:
        """Reject policies that would remove or exceed the frozen M1 bounds."""

        if not 0 < self.readiness_timeout_seconds <= M1_DENSE_INDEX_READINESS_TIMEOUT_SECONDS:
            raise ValueError("dense-index readiness timeout must be between 0 and 30 seconds")
        if not 0 < self.poll_interval_seconds <= self.readiness_timeout_seconds:
            raise ValueError("dense-index poll interval must fit inside the readiness timeout")
        if not 0 <= self.retry_delay_seconds <= self.readiness_timeout_seconds:
            raise ValueError("provider retry delay must fit inside the readiness timeout")

    def index(
        self,
        context: WorkspaceContext,
        run: ProcessingRun,
        chunks: Sequence[Chunk],
    ) -> DocumentIndexingResult:
        """Create deterministic visible vectors for one leased M1 processing run."""

        ordered_chunks = self._validate_and_order_chunks(context, run, chunks)
        vector_ids: list[DenseVectorId] = []
        batch_count = 0

        for chunk_batch in self._build_chunk_batches(ordered_chunks):
            request = EmbeddingRequest(
                inputs=tuple(
                    EmbeddingInput(
                        input_id=chunk.id,
                        text=chunk.text,
                        purpose=EmbeddingPurpose.DOCUMENT,
                    )
                    for chunk in chunk_batch
                ),
                embedding_config_version=run.embedding_config_version,
            )
            embedding_result = validate_embedding_response(
                request,
                self._embed_with_retry(request),
            )
            embeddings = embedding_result.vectors
            vectors = tuple(
                build_dense_index_vector(
                    context,
                    chunk_id=chunk.id,
                    document_version_id=chunk.document_version_id,
                    chunk_set_id=chunk.chunk_set_id,
                    embedding=embedding,
                    dense_index_config_version=run.dense_index_config_version,
                )
                for chunk, embedding in zip(chunk_batch, embeddings, strict=True)
            )
            write_request = DenseIndexWriteRequest(
                vectors=vectors,
                dense_index_config_version=run.dense_index_config_version,
            )
            write_result = self._upsert_with_retry(context, write_request)
            expected_ids = tuple(vector.vector_id for vector in vectors)
            if write_result.vector_ids != expected_ids:
                raise SearchProviderContractError(
                    "dense index provider acknowledged unexpected vector identities"
                )
            vector_ids.extend(expected_ids)
            batch_count += 1

        completed_ids = tuple(vector_ids)
        self._await_visibility(context, completed_ids)
        return DocumentIndexingResult(
            vector_ids=completed_ids,
            batch_count=batch_count,
        )

    @staticmethod
    def _validate_and_order_chunks(
        context: WorkspaceContext,
        run: ProcessingRun,
        chunks: Sequence[Chunk],
    ) -> tuple[Chunk, ...]:
        """Require one scoped leased run and one internally coherent active chunk set."""

        if run.workspace_id != context.workspace_id:
            raise DenseIndexingInputError("processing run is outside the trusted workspace")
        if run.state not in {ProcessingState.LEXICAL_READY, ProcessingState.INDEXING}:
            raise DenseIndexingInputError("dense indexing requires a ready or indexing run")
        if not run.dense_indexing_enabled:
            raise DenseIndexingInputError("processing run has dense indexing disabled")
        if run.lease_id is None:
            raise DenseIndexingInputError("dense indexing requires an owned processing lease")

        ordered_chunks = tuple(sorted(chunks, key=lambda chunk: (chunk.ordinal, chunk.id)))
        if not ordered_chunks:
            raise DenseIndexingInputError("dense indexing requires active canonical chunks")
        chunk_ids = tuple(chunk.id for chunk in ordered_chunks)
        if len(chunk_ids) != len(set(chunk_ids)):
            raise DenseIndexingInputError("active canonical chunk IDs must be unique")
        if len({chunk.chunk_set_id for chunk in ordered_chunks}) != 1:
            raise DenseIndexingInputError("active canonical chunks must share one chunk set")
        if any(
            chunk.workspace_id != context.workspace_id
            or chunk.processing_run_id != run.id
            or chunk.document_version_id != run.document_version_id
            for chunk in ordered_chunks
        ):
            raise DenseIndexingInputError(
                "active canonical chunks do not belong to the processing run"
            )
        return ordered_chunks

    @staticmethod
    def _build_chunk_batches(chunks: Sequence[Chunk]) -> tuple[tuple[Chunk, ...], ...]:
        """Pack stable batches under both the item and total-character limits."""

        batches: list[tuple[Chunk, ...]] = []
        current_batch: list[Chunk] = []
        current_characters = 0
        for chunk in chunks:
            chunk_characters = len(chunk.text)
            if chunk_characters > M1_EMBEDDING_CHARACTER_LIMIT:
                raise DenseIndexingInputError("one canonical chunk exceeds the embedding limit")
            if current_batch and (
                len(current_batch) == M1_EMBEDDING_BATCH_LIMIT
                or current_characters + chunk_characters > M1_EMBEDDING_CHARACTER_LIMIT
            ):
                batches.append(tuple(current_batch))
                current_batch = []
                current_characters = 0
            current_batch.append(chunk)
            current_characters += chunk_characters
        if current_batch:
            batches.append(tuple(current_batch))
        return tuple(batches)

    def _embed_with_retry(self, request: EmbeddingRequest) -> EmbeddingResult:
        """Own the two-attempt M1 embedding retry policy above a retry-free adapter."""

        for attempt in range(M1_PROVIDER_MAX_ATTEMPTS):
            try:
                return self.embedding_provider.embed(request)
            except SearchProviderTransientError:
                if attempt + 1 == M1_PROVIDER_MAX_ATTEMPTS:
                    raise
                self.sleeper(self.retry_delay_seconds)
        raise RuntimeError("embedding retry loop terminated without a result")

    def _upsert_with_retry(
        self,
        context: WorkspaceContext,
        request: DenseIndexWriteRequest,
    ) -> DenseIndexWriteResult:
        """Own the two-attempt M1 upsert retry policy above a retry-free adapter."""

        for attempt in range(M1_PROVIDER_MAX_ATTEMPTS):
            try:
                return self.dense_index_writer.upsert(context, request)
            except SearchProviderTransientError:
                if attempt + 1 == M1_PROVIDER_MAX_ATTEMPTS:
                    raise
                self.sleeper(self.retry_delay_seconds)
        raise RuntimeError("dense-index retry loop terminated without a result")

    def _await_visibility(
        self,
        context: WorkspaceContext,
        vector_ids: tuple[DenseVectorId, ...],
    ) -> None:
        """Poll bounded fetch batches until every deterministic identity is visible."""

        deadline = self.monotonic_clock() + self.readiness_timeout_seconds
        pending = set(vector_ids)
        transient_failures = 0
        while pending:
            requested_ids = tuple(vector_id for vector_id in vector_ids if vector_id in pending)
            try:
                for offset in range(0, len(requested_ids), M1_EMBEDDING_BATCH_LIMIT):
                    batch = requested_ids[offset : offset + M1_EMBEDDING_BATCH_LIMIT]
                    visible = tuple(self.dense_index_writer.fetch_visible(context, batch))
                    if not set(visible).issubset(batch):
                        raise SearchProviderContractError(
                            "dense index provider returned unexpected visible identities"
                        )
                    pending.difference_update(visible)
                transient_failures = 0
            except SearchProviderTransientError:
                transient_failures += 1
                if transient_failures >= M1_PROVIDER_MAX_ATTEMPTS:
                    raise

            if not pending:
                return
            remaining = deadline - self.monotonic_clock()
            if remaining <= 0:
                raise DenseIndexReadinessTimeoutError("dense index visibility deadline expired")
            self.sleeper(min(self.poll_interval_seconds, remaining))
