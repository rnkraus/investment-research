"""Application helpers for deterministic bounded lexical retrieval."""

from collections.abc import Sequence
from hashlib import sha256
from unicodedata import normalize
from uuid import UUID

from pydantic import TypeAdapter

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.contracts import (
    DenseIndexVector,
    DenseVectorId,
    EmbeddingRequest,
    EmbeddingVector,
    LexicalSearchQuery,
)
from investment_research.modules.search.domain import (
    ConfigurationVersion,
    NormalizedQuery,
    QueryText,
    RetrievalFilters,
)

QUERY_TEXT_ADAPTER = TypeAdapter(QueryText)
NORMALIZED_QUERY_ADAPTER = TypeAdapter(NormalizedQuery)
DENSE_VECTOR_ID_ADAPTER = TypeAdapter(DenseVectorId)


class EmbeddingResponseContractError(ValueError):
    """Raised when provider vectors do not match the ordered application request."""


class DenseIndexRelationshipError(ValueError):
    """Raised when an embedding cannot describe its claimed canonical chunk."""


def normalize_lexical_query(query_text: str) -> str:
    """Normalize Unicode, case, and whitespace without retaining a second raw question."""

    validated_query = QUERY_TEXT_ADAPTER.validate_python(query_text)
    canonical_unicode = normalize("NFKC", validated_query)
    collapsed_whitespace = " ".join(canonical_unicode.split())
    return NORMALIZED_QUERY_ADAPTER.validate_python(collapsed_whitespace.casefold())


def build_lexical_search_query(
    *,
    query_text: str,
    filters: RetrievalFilters,
    lexical_config_version: ConfigurationVersion,
    candidate_limit: int,
) -> LexicalSearchQuery:
    """Build the complete typed query passed to a lexical-search adapter."""

    return LexicalSearchQuery(
        normalized_query=normalize_lexical_query(query_text),
        filters=filters,
        lexical_config_version=lexical_config_version,
        candidate_limit=candidate_limit,
    )


def validate_embedding_response(
    request: EmbeddingRequest,
    vectors: Sequence[EmbeddingVector],
) -> tuple[EmbeddingVector, ...]:
    """Require exact ordered correlation and configuration before indexing."""

    validated_vectors = tuple(vectors)
    expected_input_ids = tuple(item.input_id for item in request.inputs)
    actual_input_ids = tuple(vector.input_id for vector in validated_vectors)
    if actual_input_ids != expected_input_ids:
        raise EmbeddingResponseContractError(
            "embedding response must preserve every input exactly once and in order"
        )
    if any(
        vector.embedding_config_version != request.embedding_config_version
        for vector in validated_vectors
    ):
        raise EmbeddingResponseContractError(
            "embedding response configuration version does not match the request"
        )
    return validated_vectors


def dense_index_namespace(context: WorkspaceContext) -> str:
    """Derive the only provider namespace permitted for a trusted workspace."""

    return f"ws-{context.workspace_id.hex}"


def dense_vector_id(
    context: WorkspaceContext,
    *,
    chunk_id: UUID,
    embedding_config_version: ConfigurationVersion,
) -> DenseVectorId:
    """Derive a stable vector identity from canonical workspace and chunk identity."""

    identity = f"{context.workspace_id}:{chunk_id}:{embedding_config_version}"
    digest = sha256(identity.encode("utf-8")).hexdigest()
    return DENSE_VECTOR_ID_ADAPTER.validate_python(f"v1:{digest}")


def build_dense_index_vector(
    context: WorkspaceContext,
    *,
    chunk_id: UUID,
    document_version_id: UUID,
    chunk_set_id: UUID,
    embedding: EmbeddingVector,
    dense_index_config_version: ConfigurationVersion,
) -> DenseIndexVector:
    """Map one correlated embedding to bounded provider metadata and deterministic identity."""

    if embedding.input_id != chunk_id:
        raise DenseIndexRelationshipError("embedding input must match the canonical chunk")
    return DenseIndexVector(
        vector_id=dense_vector_id(
            context,
            chunk_id=chunk_id,
            embedding_config_version=embedding.embedding_config_version,
        ),
        values=embedding.values,
        chunk_id=chunk_id,
        document_version_id=document_version_id,
        chunk_set_id=chunk_set_id,
        embedding_config_version=embedding.embedding_config_version,
        dense_index_config_version=dense_index_config_version,
    )
