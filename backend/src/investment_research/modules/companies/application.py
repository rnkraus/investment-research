"""Provider-free company creation and scoped lookup through typed transactions."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from investment_research.modules.companies.contracts import (
    CompanyListPage,
    CompanyListQuery,
    CompanySummary,
    CreateCompanyCommand,
    UpdateCompanyDetailsCommand,
)
from investment_research.modules.companies.domain import Company
from investment_research.modules.companies.ports import CompanyUnitOfWorkFactory
from investment_research.modules.identity.domain import WorkspaceContext


class CompanyUnavailableError(Exception):
    """Hide the distinction between unknown and foreign company identities."""


class CompanyService:
    """Use server-owned identity and provenance without exposing persistence models."""

    def __init__(
        self,
        *,
        write_unit_of_work_factory: CompanyUnitOfWorkFactory,
        read_unit_of_work_factory: CompanyUnitOfWorkFactory,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        identifier_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._write = write_unit_of_work_factory
        self._read = read_unit_of_work_factory
        self._clock = clock
        self._identifier_factory = identifier_factory

    def create(self, context: WorkspaceContext, command: CreateCompanyCommand) -> CompanySummary:
        """Return a safe summary only after the creation transaction has committed."""

        company = Company(
            id=self._identifier_factory(),
            workspace_id=context.workspace_id,
            name=command.name,
            ticker=command.ticker,
            created_by=context.user_id,
            created_at=self._clock(),
        )
        with self._write() as unit:
            unit.companies.add(context, company)
        return CompanySummary.from_company(company)

    def get_by_id(self, context: WorkspaceContext, company_id: UUID) -> CompanySummary:
        """Resolve an identity within the trusted workspace, without a write transaction."""

        with self._read() as unit:
            company = unit.companies.get_by_id(context, company_id)
        if company is None:
            raise CompanyUnavailableError("company is unavailable")
        return CompanySummary.from_company(company)

    def update_details(
        self, context: WorkspaceContext, company_id: UUID, command: UpdateCompanyDetailsCommand
    ) -> CompanySummary:
        with self._write() as unit:
            company = unit.companies.update_details(context, company_id, command)
            if company is None:
                raise CompanyUnavailableError("company is unavailable")
        return CompanySummary.from_company(company)

    def list_companies(self, context: WorkspaceContext, query: CompanyListQuery) -> CompanyListPage:
        """Resolve one bounded filtered page through the existing short read boundary."""

        with self._read() as unit:
            return unit.companies.list_companies(context, query)
