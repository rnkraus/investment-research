"""Unit tests for the bounded Pinecone gRPC dense-retrieval adapter."""

from dataclasses import dataclass, field
from math import inf
from typing import cast
from uuid import UUID

import pytest
from pinecone import RetryConfig
from pinecone.errors import PineconeConnectionError, UnauthorizedError
from pinecone.grpc import GrpcIndex

from investment_research.infrastructure.search import pinecone as pinecone_module
from investment_research.infrastructure.search.pinecone import (
    PINECONE_OPERATION_TIMEOUT_SECONDS,
    PineconeDenseRetriever,
    create_pinecone_dense_retriever,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.application import dense_vector_id
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_DIMENSIONS,
    DenseRetrievalQuery,
)
from investment_research.modules.search.domain import RetrievalFilters
from investment_research.modules.search.ports import (
    SearchProviderConfigurationError,
    SearchProviderContractError,
    SearchProviderTransientError,
)

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
CHUNK_ID = UUID("10000000-0000-4000-8000-000000000001")
OTHER_CHUNK_ID = UUID("10000000-0000-4000-8000-000000000002")
EMBEDDING_CONFIG_VERSION = "openai-text-embedding-3-small-1536-v1"
DENSE_INDEX_CONFIG_VERSION = "pinecone-serverless-cosine-1536-aws-us-east-1-v1"


@dataclass(frozen=True, slots=True)
class QueryMatch:
    """Minimal provider match shape consumed by the adapter."""

    id: str
    score: float
    metadata: object


@dataclass(frozen=True, slots=True)
class QueryResponse:
    """Minimal successful query response shape consumed by the adapter."""

    matches: tuple[QueryMatch, ...]


@dataclass(slots=True)
class RecordingGrpcIndex:
    """Record query calls or raise one configured SDK error."""

    query_response: QueryResponse = field(default_factory=lambda: QueryResponse(matches=()))
    error: Exception | None = None
    query_calls: list[dict[str, object]] = field(default_factory=list)
    closed: bool = False

    def query(self, **kwargs: object) -> QueryResponse:
        self.query_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.query_response

    def close(self) -> None:
        self.closed = True


def make_context() -> WorkspaceContext:
    """Build the trusted workspace context used by adapter tests."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)


def make_query(*, candidate_limit: int = 10) -> DenseRetrievalQuery:
    """Build one bounded provider-neutral query."""

    return DenseRetrievalQuery(
        query_vector=(0.125,) * M1_EMBEDDING_DIMENSIONS,
        filters=RetrievalFilters(),
        embedding_config_version=EMBEDDING_CONFIG_VERSION,
        dense_index_config_version=DENSE_INDEX_CONFIG_VERSION,
        candidate_limit=candidate_limit,
    )


def make_match(
    *,
    chunk_id: UUID = CHUNK_ID,
    score: float = -0.125,
    vector_id: str | None = None,
    metadata_changes: dict[str, object] | None = None,
) -> QueryMatch:
    """Build one response match with writer-compatible bounded metadata."""

    metadata: dict[str, object] = {
        "chunk_id": str(chunk_id),
        "embedding_config_version": EMBEDDING_CONFIG_VERSION,
        "dense_index_config_version": DENSE_INDEX_CONFIG_VERSION,
    }
    if metadata_changes:
        metadata.update(metadata_changes)
    return QueryMatch(
        id=vector_id
        or dense_vector_id(
            make_context(),
            chunk_id=chunk_id,
            embedding_config_version=EMBEDDING_CONFIG_VERSION,
        ),
        score=score,
        metadata=metadata,
    )


def make_retriever(index: RecordingGrpcIndex) -> PineconeDenseRetriever:
    """Inject a recording test double through the concrete SDK boundary."""

    return PineconeDenseRetriever(index=cast(GrpcIndex, index))


def test_retrieve_uses_workspace_namespace_versions_and_provider_order() -> None:
    first_match = make_match()
    second_match = make_match(chunk_id=OTHER_CHUNK_ID, score=0.25)
    index = RecordingGrpcIndex(query_response=QueryResponse(matches=(first_match, second_match)))
    query = make_query(candidate_limit=2)

    hits = make_retriever(index).retrieve(make_context(), query)

    assert [(hit.chunk_id, hit.raw_score) for hit in hits] == [
        (CHUNK_ID, -0.125),
        (OTHER_CHUNK_ID, 0.25),
    ]
    assert index.query_calls == [
        {
            "top_k": 2,
            "vector": list(query.query_vector),
            "namespace": f"ws-{WORKSPACE_ID.hex}",
            "filter": {
                "embedding_config_version": {"$eq": EMBEDDING_CONFIG_VERSION},
                "dense_index_config_version": {"$eq": DENSE_INDEX_CONFIG_VERSION},
            },
            "include_values": False,
            "include_metadata": True,
            "timeout": PINECONE_OPERATION_TIMEOUT_SECONDS,
        }
    ]


@pytest.mark.parametrize(
    ("match", "expected_error"),
    [
        pytest.param(
            QueryMatch(id="v1:" + "0" * 64, score=0.1, metadata=None),
            "required metadata",
            id="missing-metadata",
        ),
        pytest.param(
            make_match(metadata_changes={"chunk_id": "not-a-uuid"}),
            "malformed match",
            id="malformed-chunk-id",
        ),
        pytest.param(
            make_match(metadata_changes={"embedding_config_version": "other-version"}),
            "inconsistent metadata",
            id="wrong-embedding-version",
        ),
        pytest.param(
            make_match(vector_id="v1:" + "f" * 64),
            "inconsistent vector identity",
            id="wrong-vector-id",
        ),
        pytest.param(
            make_match(score=inf),
            "malformed match",
            id="non-finite-score",
        ),
    ],
)
def test_retrieve_rejects_malformed_or_inconsistent_matches(
    match: QueryMatch,
    expected_error: str,
) -> None:
    index = RecordingGrpcIndex(query_response=QueryResponse(matches=(match,)))

    with pytest.raises(SearchProviderContractError, match=expected_error):
        make_retriever(index).retrieve(make_context(), make_query())


def test_retrieve_rejects_duplicate_chunks_and_excess_matches() -> None:
    duplicate_index = RecordingGrpcIndex(
        query_response=QueryResponse(matches=(make_match(), make_match()))
    )
    with pytest.raises(SearchProviderContractError, match="duplicate chunk"):
        make_retriever(duplicate_index).retrieve(make_context(), make_query())

    excess_index = RecordingGrpcIndex(
        query_response=QueryResponse(matches=(make_match(), make_match(chunk_id=OTHER_CHUNK_ID)))
    )
    with pytest.raises(SearchProviderContractError, match="candidate limit"):
        make_retriever(excess_index).retrieve(
            make_context(),
            make_query(candidate_limit=1),
        )


@pytest.mark.parametrize(
    ("provider_error", "application_error"),
    [
        pytest.param(
            PineconeConnectionError("sensitive transport detail"),
            SearchProviderTransientError,
            id="connection",
        ),
        pytest.param(
            UnauthorizedError("sensitive authentication detail"),
            SearchProviderConfigurationError,
            id="authentication",
        ),
    ],
)
def test_provider_errors_are_translated_without_exposing_details(
    provider_error: Exception,
    application_error: type[Exception],
) -> None:
    index = RecordingGrpcIndex(error=provider_error)

    with pytest.raises(application_error) as captured:
        make_retriever(index).retrieve(make_context(), make_query())

    assert "sensitive" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_factory_enforces_tls_timeout_and_single_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class RecordingConstructor:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(pinecone_module, "GrpcIndex", RecordingConstructor)

    retriever = create_pinecone_dense_retriever(
        api_key="synthetic-query-key",
        index_host="synthetic-index.example.svc.pinecone.io",
    )

    assert retriever.operation_timeout_seconds == PINECONE_OPERATION_TIMEOUT_SECONDS
    assert captured["secure"] is True
    assert captured["grpc_scheme"] == "https"
    assert captured["timeout"] == PINECONE_OPERATION_TIMEOUT_SECONDS
    assert captured["connect_timeout"] == 5.0
    retry_config = captured["retry_config"]
    assert isinstance(retry_config, RetryConfig)
    assert retry_config.max_retries == 0


def test_close_releases_grpc_index() -> None:
    index = RecordingGrpcIndex()
    retriever = make_retriever(index)

    retriever.close()

    assert index.closed is True
