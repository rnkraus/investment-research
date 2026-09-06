"""Application-owned persistence contracts for reproducible retrieval."""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.domain import RetrievalCandidate, RetrievalRun


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
