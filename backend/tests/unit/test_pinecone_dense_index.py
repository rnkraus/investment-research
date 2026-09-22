"""Unit tests for the bounded Pinecone gRPC dense-index adapter."""

from dataclasses import dataclass, field
from typing import cast
from uuid import UUID

import pytest
from pinecone import RetryConfig
from pinecone.errors import PineconeConnectionError, UnauthorizedError
from pinecone.grpc import GrpcIndex
from pinecone.models.vectors.responses import UpsertResponse

from investment_research.infrastructure.search import pinecone as pinecone_module
from investment_research.infrastructure.search.pinecone import (
    PINECONE_OPERATION_TIMEOUT_SECONDS,
    PineconeDenseIndexWriter,
    create_pinecone_dense_index_writer,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.application import dense_vector_id
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_DIMENSIONS,
    DenseIndexVector,
    DenseIndexWriteRequest,
    DenseVectorId,
)
from investment_research.modules.search.ports import (
    SearchProviderConfigurationError,
    SearchProviderContractError,
    SearchProviderTransientError,
)

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
CHUNK_ID = UUID("10000000-0000-4000-8000-000000000001")
OTHER_CHUNK_ID = UUID("10000000-0000-4000-8000-000000000002")
DOCUMENT_VERSION_ID = UUID("20000000-0000-4000-8000-000000000001")
CHUNK_SET_ID = UUID("30000000-0000-4000-8000-000000000001")
EMBEDDING_CONFIG_VERSION = "openai-text-embedding-3-small-1536-v1"
DENSE_INDEX_CONFIG_VERSION = "pinecone-serverless-cosine-1536-aws-us-east-1-v1"


@dataclass(frozen=True, slots=True)
class FetchResponse:
    """Minimal fetch response shape consumed by the adapter."""

    vectors: dict[str, object]


@dataclass(slots=True)
class RecordingGrpcIndex:
    """Record adapter calls or raise one configured SDK error."""

    upsert_response: UpsertResponse = field(
        default_factory=lambda: UpsertResponse(upserted_count=1)
    )
    fetch_response: FetchResponse = field(default_factory=lambda: FetchResponse(vectors={}))
    error: Exception | None = None
    upsert_calls: list[dict[str, object]] = field(default_factory=list)
    fetch_calls: list[dict[str, object]] = field(default_factory=list)
    closed: bool = False

    def upsert(self, **kwargs: object) -> UpsertResponse:
        self.upsert_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.upsert_response

    def fetch(self, **kwargs: object) -> FetchResponse:
        self.fetch_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.fetch_response

    def close(self) -> None:
        self.closed = True


def make_context() -> WorkspaceContext:
    """Build the trusted workspace context used by adapter tests."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)


def make_vector(chunk_id: UUID = CHUNK_ID) -> DenseIndexVector:
    """Build one fully validated deterministic dense-index vector."""

    context = make_context()
    return DenseIndexVector(
        vector_id=dense_vector_id(
            context,
            chunk_id=chunk_id,
            embedding_config_version=EMBEDDING_CONFIG_VERSION,
        ),
        values=(0.125,) * M1_EMBEDDING_DIMENSIONS,
        chunk_id=chunk_id,
        document_version_id=DOCUMENT_VERSION_ID,
        chunk_set_id=CHUNK_SET_ID,
        embedding_config_version=EMBEDDING_CONFIG_VERSION,
        dense_index_config_version=DENSE_INDEX_CONFIG_VERSION,
    )


def make_request(*vectors: DenseIndexVector) -> DenseIndexWriteRequest:
    """Build one coherent provider-neutral write request."""

    return DenseIndexWriteRequest(
        vectors=vectors or (make_vector(),),
        dense_index_config_version=DENSE_INDEX_CONFIG_VERSION,
    )


def make_writer(index: RecordingGrpcIndex) -> PineconeDenseIndexWriter:
    """Inject a recording test double through the concrete SDK boundary."""

    return PineconeDenseIndexWriter(index=cast(GrpcIndex, index))


def test_upsert_uses_workspace_namespace_and_bounded_metadata() -> None:
    index = RecordingGrpcIndex()
    writer = make_writer(index)
    vector = make_vector()

    result = writer.upsert(make_context(), make_request(vector))

    assert result.vector_ids == (vector.vector_id,)
    assert index.upsert_calls == [
        {
            "vectors": [
                {
                    "id": vector.vector_id,
                    "values": list(vector.values),
                    "metadata": {
                        "chunk_id": str(CHUNK_ID),
                        "document_version_id": str(DOCUMENT_VERSION_ID),
                        "chunk_set_id": str(CHUNK_SET_ID),
                        "embedding_config_version": EMBEDDING_CONFIG_VERSION,
                        "dense_index_config_version": DENSE_INDEX_CONFIG_VERSION,
                    },
                }
            ],
            "namespace": f"ws-{WORKSPACE_ID.hex}",
            "timeout": PINECONE_OPERATION_TIMEOUT_SECONDS,
        }
    ]


def test_fetch_visible_preserves_request_order_and_rejects_extra_ids() -> None:
    first_id = make_vector().vector_id
    second_id = make_vector(OTHER_CHUNK_ID).vector_id
    index = RecordingGrpcIndex(fetch_response=FetchResponse(vectors={second_id: object()}))
    writer = make_writer(index)

    assert writer.fetch_visible(make_context(), (first_id, second_id)) == (second_id,)
    assert index.fetch_calls == [
        {
            "ids": [first_id, second_id],
            "namespace": f"ws-{WORKSPACE_ID.hex}",
            "timeout": PINECONE_OPERATION_TIMEOUT_SECONDS,
        }
    ]

    index.fetch_response = FetchResponse(vectors={"v1:" + "f" * 64: object()})
    with pytest.raises(SearchProviderContractError, match="unrequested"):
        writer.fetch_visible(make_context(), (first_id, second_id))


def test_fetch_visible_caps_sdk_timeout_to_remaining_deadline() -> None:
    vector_id = make_vector().vector_id
    index = RecordingGrpcIndex()
    writer = make_writer(index)

    assert writer.fetch_visible(make_context(), (vector_id,), timeout_seconds=0.4) == ()
    assert index.fetch_calls[0]["timeout"] == 0.4

    writer.fetch_visible(make_context(), (vector_id,), timeout_seconds=30.0)
    assert index.fetch_calls[1]["timeout"] == PINECONE_OPERATION_TIMEOUT_SECONDS

    with pytest.raises(ValueError, match="timeout must be positive"):
        writer.fetch_visible(make_context(), (vector_id,), timeout_seconds=0.0)
    assert len(index.fetch_calls) == 2


def test_upsert_rejects_incomplete_direct_provider_acknowledgement() -> None:
    index = RecordingGrpcIndex(upsert_response=UpsertResponse(upserted_count=0))

    with pytest.raises(SearchProviderContractError, match="complete write"):
        make_writer(index).upsert(make_context(), make_request())


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
        make_writer(index).upsert(make_context(), make_request())

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

    writer = create_pinecone_dense_index_writer(
        api_key="synthetic-test-key",
        index_host="synthetic-index.example.svc.pinecone.io",
    )

    assert writer.operation_timeout_seconds == PINECONE_OPERATION_TIMEOUT_SECONDS
    assert captured["secure"] is True
    assert captured["grpc_scheme"] == "https"
    assert captured["timeout"] == PINECONE_OPERATION_TIMEOUT_SECONDS
    assert captured["connect_timeout"] == 5.0
    retry_config = captured["retry_config"]
    assert isinstance(retry_config, RetryConfig)
    assert retry_config.max_retries == 0


def test_close_releases_grpc_index() -> None:
    index = RecordingGrpcIndex()
    writer = make_writer(index)

    writer.close()

    assert index.closed is True


@pytest.mark.parametrize(
    "vector_ids",
    [
        pytest.param((), id="empty"),
        pytest.param(
            tuple(f"v1:{index:064x}" for index in range(17)),
            id="too-many",
        ),
    ],
)
def test_fetch_visible_rejects_unbounded_requests(
    vector_ids: tuple[DenseVectorId, ...],
) -> None:
    with pytest.raises(ValueError, match=r"requires|exceeds"):
        make_writer(RecordingGrpcIndex()).fetch_visible(make_context(), vector_ids)
