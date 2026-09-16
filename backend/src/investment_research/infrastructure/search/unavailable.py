"""Explicitly unavailable adapters for search providers outside the M0 scope."""

from collections.abc import Sequence

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.contracts import (
    DenseRetrievalHit,
    DenseRetrievalQuery,
    EmbeddingRequest,
    EmbeddingVector,
    RerankRequest,
    RerankResult,
)
from investment_research.modules.search.ports import SearchProviderUnavailableError


class UnavailableEmbeddingProvider:
    """Reject embedding calls until an approved provider is configured."""

    def embed(self, request: EmbeddingRequest) -> Sequence[EmbeddingVector]:
        """Fail explicitly without contacting a provider."""

        del request
        raise SearchProviderUnavailableError("embedding provider is unavailable")


class UnavailableDenseRetriever:
    """Reject dense retrieval until an approved vector index is configured."""

    def retrieve(
        self,
        context: WorkspaceContext,
        query: DenseRetrievalQuery,
    ) -> Sequence[DenseRetrievalHit]:
        """Fail explicitly without contacting a vector index."""

        del context, query
        raise SearchProviderUnavailableError("dense retrieval provider is unavailable")


class UnavailableReranker:
    """Reject reranking until an approved provider is configured."""

    def rerank(self, request: RerankRequest) -> Sequence[RerankResult]:
        """Fail explicitly without contacting a reranking provider."""

        del request
        raise SearchProviderUnavailableError("reranking provider is unavailable")
