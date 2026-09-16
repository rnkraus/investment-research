"""Application-owned persistence contracts for reproducible retrieval."""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.contracts import (
    DenseRetrievalHit,
    DenseRetrievalQuery,
    EmbeddingRequest,
    EmbeddingVector,
    LexicalSearchHit,
    LexicalSearchQuery,
    RerankRequest,
    RerankResult,
)
from investment_research.modules.search.domain import RetrievalCandidate, RetrievalRun


class SearchProviderUnavailableError(RuntimeError):
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


class DenseRetriever(Protocol):
    """Select bounded vector candidates inside one trusted workspace."""

    def retrieve(
        self,
        context: WorkspaceContext,
        query: DenseRetrievalQuery,
    ) -> Sequence[DenseRetrievalHit]:
        """Return provider hits that still require canonical eligibility checks."""


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
