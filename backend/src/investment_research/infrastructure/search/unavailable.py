"""Explicitly unavailable adapters for search providers outside the M0 scope."""

from collections.abc import Sequence

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.contracts import (
    DenseIndexWriteRequest,
    DenseIndexWriteResult,
    DenseRetrievalHit,
    DenseRetrievalQuery,
    DenseVectorId,
    EmbeddingRequest,
    EmbeddingResult,
    RerankRequest,
    RerankResult,
)
from investment_research.modules.search.ports import SearchProviderUnavailableError


class UnavailableEmbeddingProvider:
    """Reject embedding calls until an approved provider is configured."""

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        """Fail explicitly without contacting a provider."""

        del request
        raise SearchProviderUnavailableError("embedding provider is unavailable")


class UnavailableDenseIndexWriter:
    """Reject dense-index writes until an approved vector index is configured."""

    def upsert(
        self,
        context: WorkspaceContext,
        request: DenseIndexWriteRequest,
    ) -> DenseIndexWriteResult:
        """Fail explicitly without contacting a vector index."""

        del context, request
        raise SearchProviderUnavailableError("dense index writer is unavailable")

    def fetch_visible(
        self,
        context: WorkspaceContext,
        vector_ids: Sequence[DenseVectorId],
    ) -> Sequence[DenseVectorId]:
        """Fail explicitly without contacting a vector index."""

        del context, vector_ids
        raise SearchProviderUnavailableError("dense index writer is unavailable")


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
