"""Application-owned persistence contracts for reproducible retrieval."""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.contracts import (
    DenseIndexWriteRequest,
    DenseIndexWriteResult,
    DenseQualificationRequest,
    DenseRetrievalHit,
    DenseRetrievalQuery,
    DenseVectorId,
    EmbeddingRequest,
    EmbeddingVector,
    LexicalSearchHit,
    LexicalSearchQuery,
    QualifiedDenseHit,
    RerankRequest,
    RerankResult,
)
from investment_research.modules.search.domain import RetrievalCandidate, RetrievalRun


class SearchProviderError(RuntimeError):
    """Base error for safe application-owned provider failure categories."""


class SearchProviderTransientError(SearchProviderError):
    """Report a bounded provider failure that a later application attempt may retry."""


class SearchProviderContractError(SearchProviderError):
    """Report a malformed, partial, or contradictory provider response."""


class SearchProviderConfigurationError(SearchProviderError):
    """Report provider authentication or configuration that retries cannot repair."""


class SearchProviderUnavailableError(SearchProviderError):
    """Raised when a later-milestone search provider is intentionally unavailable."""


class LexicalSearch(Protocol):
    """Select bounded, eligible PostgreSQL full-text candidates."""

    def search(
        self,
        context: WorkspaceContext,
        query: LexicalSearchQuery,
    ) -> Sequence[LexicalSearchHit]:
        """Return eligible hits in deterministic descending relevance order."""


class EmbeddingProvider(Protocol):
    """Create correlated query or document vectors through an external provider."""

    def embed(self, request: EmbeddingRequest) -> Sequence[EmbeddingVector]:
        """Return one ordered vector for every input or raise a provider error."""


class DenseIndexWriter(Protocol):
    """Write and verify derived vectors inside one trusted workspace namespace."""

    def upsert(
        self,
        context: WorkspaceContext,
        request: DenseIndexWriteRequest,
    ) -> DenseIndexWriteResult:
        """Idempotently write deterministic vector identities."""

    def fetch_visible(
        self,
        context: WorkspaceContext,
        vector_ids: Sequence[DenseVectorId],
    ) -> Sequence[DenseVectorId]:
        """Return only requested vector identities currently visible in the index."""


class DenseRetriever(Protocol):
    """Select bounded vector candidates inside one trusted workspace."""

    def retrieve(
        self,
        context: WorkspaceContext,
        query: DenseRetrievalQuery,
    ) -> Sequence[DenseRetrievalHit]:
        """Return provider hits that still require canonical eligibility checks."""


class DenseCandidateQualifier(Protocol):
    """Rehydrate provider hits only through eligible canonical PostgreSQL state."""

    def qualify(
        self,
        context: WorkspaceContext,
        request: DenseQualificationRequest,
    ) -> Sequence[QualifiedDenseHit]:
        """Preserve provider order while silently excluding ineligible identifiers."""


class Reranker(Protocol):
    """Reorder a bounded canonical candidate set without creating evidence."""

    def rerank(self, request: RerankRequest) -> Sequence[RerankResult]:
        """Return one uniquely ranked finite score for every candidate."""


class RetrievalRepository(Protocol):
    """Persist and read retrieval records inside one trusted workspace."""

    def add_result(
        self,
        context: WorkspaceContext,
        run: RetrievalRun,
        candidates: Sequence[RetrievalCandidate],
    ) -> None:
        """Stage one completed retrieval run and its ordered candidates."""

    def get_run_by_id(
        self,
        context: WorkspaceContext,
        retrieval_run_id: UUID,
    ) -> RetrievalRun | None:
        """Return a scoped run without disclosing inaccessible identifiers."""

    def list_candidates(
        self,
        context: WorkspaceContext,
        retrieval_run_id: UUID,
    ) -> Sequence[RetrievalCandidate]:
        """Return candidates in deterministic retrieval order for a scoped run."""
