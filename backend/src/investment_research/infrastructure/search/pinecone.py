"""Pinecone gRPC adapter for bounded workspace-scoped dense-index writes."""

from collections.abc import Sequence
from dataclasses import dataclass

from pinecone import RetryConfig
from pinecone.errors import (
    PineconeConnectionError,
    PineconeError,
    PineconeTimeoutError,
    RateLimitError,
    RateLimitException,
    ServiceError,
)
from pinecone.grpc import GrpcIndex

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.application import (
    DENSE_VECTOR_ID_ADAPTER,
    dense_index_namespace,
)
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_BATCH_LIMIT,
    DenseIndexWriteRequest,
    DenseIndexWriteResult,
    DenseVectorId,
)
from investment_research.modules.search.ports import (
    SearchProviderConfigurationError,
    SearchProviderContractError,
    SearchProviderTransientError,
)

PINECONE_OPERATION_TIMEOUT_SECONDS = 20.0
PINECONE_CONNECT_TIMEOUT_SECONDS = 5.0

_TRANSIENT_PINECONE_ERRORS = (
    PineconeConnectionError,
    PineconeTimeoutError,
    RateLimitError,
    RateLimitException,
    ServiceError,
)


@dataclass(frozen=True, slots=True)
class PineconeDenseIndexWriter:
    """Write deterministic vectors through one injected Pinecone gRPC data client."""

    index: GrpcIndex
    operation_timeout_seconds: float = PINECONE_OPERATION_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not 0 < self.operation_timeout_seconds <= PINECONE_OPERATION_TIMEOUT_SECONDS:
            raise ValueError("Pinecone operation timeout must be between 0 and 20 seconds")

    def upsert(
        self,
        context: WorkspaceContext,
        request: DenseIndexWriteRequest,
    ) -> DenseIndexWriteResult:
        """Write one validated batch without exposing source text as metadata."""

        vector_ids = tuple(vector.vector_id for vector in request.vectors)
        vectors = [
            {
                "id": vector.vector_id,
                "values": list(vector.values),
                "metadata": {
                    "chunk_id": str(vector.chunk_id),
                    "document_version_id": str(vector.document_version_id),
                    "chunk_set_id": str(vector.chunk_set_id),
                    "embedding_config_version": vector.embedding_config_version,
                    "dense_index_config_version": vector.dense_index_config_version,
                },
            }
            for vector in request.vectors
        ]
        try:
            response = self.index.upsert(
                vectors=vectors,
                namespace=dense_index_namespace(context),
                max_concurrency=1,
                show_progress=False,
                timeout=self.operation_timeout_seconds,
                total_timeout=self.operation_timeout_seconds,
            )
        except _TRANSIENT_PINECONE_ERRORS:
            raise SearchProviderTransientError(
                "dense index provider is temporarily unavailable"
            ) from None
        except PineconeError:
            raise SearchProviderConfigurationError(
                "dense index provider rejected the configured request"
            ) from None

        if (
            response.upserted_count != len(vector_ids)
            or response.total_item_count != len(vector_ids)
            or response.failed_item_count != 0
            or response.errors
        ):
            raise SearchProviderContractError(
                "dense index provider did not acknowledge the complete batch"
            )
        return DenseIndexWriteResult(vector_ids=vector_ids)

    def fetch_visible(
        self,
        context: WorkspaceContext,
        vector_ids: Sequence[DenseVectorId],
    ) -> Sequence[DenseVectorId]:
        """Return visible requested IDs in request order and reject foreign response IDs."""

        requested_ids = tuple(
            DENSE_VECTOR_ID_ADAPTER.validate_python(vector_id) for vector_id in vector_ids
        )
        if not requested_ids:
            raise ValueError("dense index visibility check requires vector IDs")
        if len(requested_ids) > M1_EMBEDDING_BATCH_LIMIT:
            raise ValueError("dense index visibility check exceeds the batch limit")
        if len(requested_ids) != len(set(requested_ids)):
            raise ValueError("dense index visibility check requires unique vector IDs")

        try:
            response = self.index.fetch(
                ids=list(requested_ids),
                namespace=dense_index_namespace(context),
                timeout=self.operation_timeout_seconds,
            )
        except _TRANSIENT_PINECONE_ERRORS:
            raise SearchProviderTransientError(
                "dense index provider is temporarily unavailable"
            ) from None
        except PineconeError:
            raise SearchProviderConfigurationError(
                "dense index provider rejected the configured request"
            ) from None

        visible_ids = set(response.vectors)
        unexpected_ids = visible_ids.difference(requested_ids)
        if unexpected_ids:
            raise SearchProviderContractError(
                "dense index provider returned unrequested vector identities"
            )
        return tuple(vector_id for vector_id in requested_ids if vector_id in visible_ids)

    def close(self) -> None:
        """Release the SDK-owned gRPC channel."""

        self.index.close()


def create_pinecone_dense_index_writer(
    *,
    api_key: str,
    index_host: str,
    operation_timeout_seconds: float = PINECONE_OPERATION_TIMEOUT_SECONDS,
) -> PineconeDenseIndexWriter:
    """Build the least-privilege gRPC writer without a control-plane lookup."""

    if not api_key.strip():
        raise ValueError("Pinecone API key must not be empty")
    if not index_host.strip():
        raise ValueError("Pinecone index host must not be empty")
    if not 0 < operation_timeout_seconds <= PINECONE_OPERATION_TIMEOUT_SECONDS:
        raise ValueError("Pinecone operation timeout must be between 0 and 20 seconds")

    index = GrpcIndex(
        host=index_host,
        api_key=api_key,
        secure=True,
        grpc_scheme="https",
        timeout=operation_timeout_seconds,
        connect_timeout=min(PINECONE_CONNECT_TIMEOUT_SECONDS, operation_timeout_seconds),
        retry_config=RetryConfig(max_retries=0),
    )
    return PineconeDenseIndexWriter(
        index=index,
        operation_timeout_seconds=operation_timeout_seconds,
    )
