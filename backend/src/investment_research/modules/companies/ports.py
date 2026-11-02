"""Application-owned storage boundary for workspace-scoped company identity."""

from typing import Protocol
from uuid import UUID

from investment_research.modules.companies.domain import Company, CompanyName
from investment_research.modules.identity.domain import WorkspaceContext


class CompanyRepository(Protocol):
    """Stage company writes and scoped reads inside a caller-owned transaction."""

    def add(self, context: WorkspaceContext, company: Company) -> None:
        """Stage company creation without committing or trusting a submitted scope."""

    def get_by_id(self, context: WorkspaceContext, company_id: UUID) -> Company | None:
        """Return a scoped company or the same absence for unknown and foreign IDs."""

    def get_by_name(self, context: WorkspaceContext, name: CompanyName) -> Company | None:
        """Resolve an exact normalized name within the trusted workspace."""
