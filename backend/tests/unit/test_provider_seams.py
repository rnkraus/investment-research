"""Unit tests for M0 unavailable-provider and no-op tracing adapters."""

from uuid import UUID

import pytest

from investment_research.infrastructure.operations.noop import NoopOperationTracer
from investment_research.infrastructure.search.unavailable import (
    UnavailableDenseRetriever,
    UnavailableEmbeddingProvider,
    UnavailableReranker,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.ports import OperationTracer
from investment_research.modules.search.contracts import (
    DenseRetrievalQuery,
    EmbeddingInput,
    EmbeddingPurpose,
    EmbeddingRequest,
    RerankCandidate,
    RerankRequest,
)
from investment_research.modules.search.domain import RetrievalFilters
from investment_research.modules.search.ports import (
    DenseRetriever,
    EmbeddingProvider,
    Reranker,
    SearchProviderUnavailableError,
)

USER_ID = UUID("00000000-0000-4000-8000-000000000311")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000312")
INPUT_ID = UUID("00000000-0000-4000-8000-000000000313")
CANDIDATE_ID = UUID("00000000-0000-4000-8000-000000000314")


def test_unavailable_search_adapters_fail_without_provider_calls() -> None:
    embedding_provider: EmbeddingProvider = UnavailableEmbeddingProvider()
    dense_retriever: DenseRetriever = UnavailableDenseRetriever()
    reranker: Reranker = UnavailableReranker()
    context = WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)

    with pytest.raises(SearchProviderUnavailableError, match="embedding provider"):
        embedding_provider.embed(
            EmbeddingRequest(
                inputs=(
                    EmbeddingInput(
                        input_id=INPUT_ID,
                        text="What changed?",
                        purpose=EmbeddingPurpose.QUERY,
                    ),
                ),
                embedding_config_version="embedding-v1",
            )
        )
    with pytest.raises(SearchProviderUnavailableError, match="dense retrieval provider"):
        dense_retriever.retrieve(
            context,
            DenseRetrievalQuery(
                query_vector=(0.25, -0.5),
                filters=RetrievalFilters(),
                embedding_config_version="embedding-v1",
                dense_config_version="dense-v1",
                candidate_limit=10,
            ),
        )
    with pytest.raises(SearchProviderUnavailableError, match="reranking provider"):
        reranker.rerank(
            RerankRequest(
                normalized_query="what changed?",
                candidates=(
                    RerankCandidate(
                        retrieval_candidate_id=CANDIDATE_ID,
                        text="Canonical candidate text",
                    ),
                ),
                reranker_config_version="reranker-v1",
            )
        )


def test_noop_tracer_accepts_events_without_changing_application_behavior() -> None:
    tracer: OperationTracer = NoopOperationTracer()

    with tracer.start_span("research.retrieve", attributes={"candidate_limit": 10}) as span:
        span.set_attribute("workspace_type", "sanitized_demo")
        span.record_error("provider_timeout")

    with (
        pytest.raises(RuntimeError, match="application failure"),
        tracer.start_span("research.generate"),
    ):
        raise RuntimeError("application failure")
