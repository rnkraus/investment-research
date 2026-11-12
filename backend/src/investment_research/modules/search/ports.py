"""Application-owned persistence contracts for reproducible retrieval."""

from collections.abc import Sequence
from datetime import date
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
    EmbeddingResult,
    LexicalSearchHit,
    LexicalSearchQuery,
    QualifiedDenseHit,
    RerankRequest,
    RerankResponse,
)
from investment_research.modules.search.domain import RetrievalCandidate, RetrievalRun
from investment_research.modules.search.text_neighbors import TextNeighbor


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

    def text_neighbors(
        self,
        context: WorkspaceContext,
        query: LexicalSearchQuery,
        anchors: Sequence[UUID],
        *,
        embedding_config_version: str,
        dense_index_config_version: str,
    ) -> Sequence[TextNeighbor]:
        """Read neighboring chunks from the same eligible text chunk set."""

    def search_pages(
        self, context: WorkspaceContext, query: LexicalSearchQuery, index: DenseQualificationRequest
    ) -> Sequence[LexicalSearchHit]:
        """Rank full pages and text documents within the active archive scope."""

    def search_observation(
        self,
        context: WorkspaceContext,
        query: LexicalSearchQuery,
        index: DenseQualificationRequest,
        metric: str,
        observation_date: date,
    ) -> LexicalSearchHit | None:
        """Select a metric passage that also contains the requested observation date."""


class EmbeddingProvider(Protocol):
    """Create correlated query or document vectors through an external provider."""

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        """Return ordered vectors and usage for one call or raise a provider error."""


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
        *,
        timeout_seconds: float | None = None,
    ) -> Sequence[DenseVectorId]:
        """Return requested visible IDs, bounding a blocking fetch when a timeout is given."""


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

    def current_document_version_ids(
        self, context: WorkspaceContext, document_ids: tuple[UUID, ...]
    ) -> tuple[UUID, ...]:
        """Resolve selected active documents before the provider applies its result limit."""

    def qualify(
        self,
        context: WorkspaceContext,
        request: DenseQualificationRequest,
    ) -> Sequence[QualifiedDenseHit]:
        """Preserve provider order while silently excluding ineligible identifiers."""


class Reranker(Protocol):
    """Reorder a bounded canonical candidate set without creating evidence."""

    def rerank(self, request: RerankRequest) -> RerankResponse:
        """Return every candidate with a finite unique rank, model, version, and usage."""


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
