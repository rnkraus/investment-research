"""Read and replace a document's manually reviewed company set."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from investment_research.modules.companies.contracts import CompanySummary
from investment_research.modules.identity.domain import WorkspaceContext

COMPANY_ASSIGNMENT_LIMIT = 20
MAX_ASSIGNMENT_REVISION = 2**63 - 1


class ReplaceDocumentCompaniesCommand(BaseModel):
    """Replace the complete set using the version and revision observed by the caller."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    expected_document_version_id: UUID
    expected_revision: int = Field(strict=True, ge=0, le=MAX_ASSIGNMENT_REVISION)
    company_ids: tuple[UUID, ...] = Field(max_length=COMPANY_ASSIGNMENT_LIMIT)

    @model_validator(mode="after")
    def require_distinct_companies(self) -> Self:
        if len(self.company_ids) != len(set(self.company_ids)):
            raise ValueError("company assignments require distinct company IDs")
        return self


class DocumentCompanyAssignment(BaseModel):
    """A coherent snapshot of the current document and its company links."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    document_id: UUID
    document_version_id: UUID
    revision: int = Field(ge=0, le=MAX_ASSIGNMENT_REVISION)
    companies: tuple[CompanySummary, ...] = Field(max_length=COMPANY_ASSIGNMENT_LIMIT)


class CompanyAssignmentUnavailableError(Exception):
    """Return the same failure for missing, foreign, or excluded targets."""


class CompanyAssignmentConflictError(Exception):
    """Require the caller to reload a stale or exhausted edit revision."""


class DocumentCompanyRepository(Protocol):
    def read(
        self, context: WorkspaceContext, document_id: UUID, *, for_update: bool = False
    ) -> DocumentCompanyAssignment | None: ...

    def find_companies(
        self, context: WorkspaceContext, company_ids: tuple[UUID, ...]
    ) -> tuple[CompanySummary, ...]: ...

    def apply_changes(
        self,
        context: WorkspaceContext,
        document_id: UUID,
        *,
        removed: set[UUID],
        added: set[UUID],
        revision: int,
        reviewed_at: datetime,
    ) -> None: ...


type DocumentCompanyTransaction = Callable[[], AbstractContextManager[DocumentCompanyRepository]]


class DocumentCompanyService:
    def __init__(
        self,
        transaction: DocumentCompanyTransaction,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._transaction = transaction
        self._clock = clock or (lambda: datetime.now(UTC))

    def get(self, context: WorkspaceContext, document_id: UUID) -> DocumentCompanyAssignment:
        with self._transaction() as repository:
            assignment = repository.read(context, document_id)
            if assignment is None:
                raise CompanyAssignmentUnavailableError("document is unavailable")
        return assignment

    def replace(
        self, context: WorkspaceContext, document_id: UUID, command: ReplaceDocumentCompaniesCommand
    ) -> DocumentCompanyAssignment:
        with self._transaction() as repository:
            current = repository.read(context, document_id, for_update=True)
            if current is None:
                raise CompanyAssignmentUnavailableError("document is unavailable")
            if (
                current.document_version_id != command.expected_document_version_id
                or current.revision != command.expected_revision
            ):
                raise CompanyAssignmentConflictError("company assignment has changed")

            previous_ids = {company.company_id for company in current.companies}
            requested_ids = set(command.company_ids)
            if previous_ids == requested_ids:
                return current
            companies = repository.find_companies(context, command.company_ids)
            if len(companies) != len(requested_ids):
                raise CompanyAssignmentUnavailableError("company is unavailable")
            if current.revision == MAX_ASSIGNMENT_REVISION:
                raise CompanyAssignmentConflictError("company assignment revision is exhausted")
            revision = current.revision + 1
            repository.apply_changes(
                context,
                document_id,
                removed=previous_ids - requested_ids,
                added=requested_ids - previous_ids,
                revision=revision,
                reviewed_at=self._clock(),
            )
            return DocumentCompanyAssignment(
                document_id=document_id,
                document_version_id=current.document_version_id,
                revision=revision,
                companies=companies,
            )
