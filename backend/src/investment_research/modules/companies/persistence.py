"""SQLAlchemy mapping and scoped repository for canonical company identities."""

from datetime import datetime
from typing import Self
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    func,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, Session, mapped_column

from investment_research.infrastructure.database.base import Base
from investment_research.modules.companies.contracts import (
    CompanyListCursor,
    CompanyListPage,
    CompanyListQuery,
    CompanySummary,
)
from investment_research.modules.companies.domain import (
    COMPANY_NAME_KEY_MAX_LENGTH,
    COMPANY_NAME_MAX_LENGTH,
    COMPANY_TICKER_MAX_LENGTH,
    Company,
    CompanyName,
    CompanyNameIntegrityError,
    CompanyScopeError,
    CompanyTicker,
    company_name_key,
)
from investment_research.modules.companies.ports import InvalidCompanyCursorError
from investment_research.modules.identity.domain import WorkspaceContext


class CompanyRow(Base):
    """Persist one company with scoped uniqueness on its application-derived name key."""

    __tablename__ = "companies"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "normalized_name"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        CheckConstraint("char_length(name) > 0 AND name = btrim(name)", name="non_empty_name"),
        CheckConstraint("ticker ~ '^[A-Z0-9][A-Z0-9.-]*$'", name="ticker_format"),
        CheckConstraint(
            "char_length(normalized_name) > 0 AND normalized_name = btrim(normalized_name)",
            name="non_empty_normalized_name",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(COMPANY_NAME_MAX_LENGTH), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(COMPANY_TICKER_MAX_LENGTH, collation="C"))
    normalized_name: Mapped[str] = mapped_column(
        String(COMPANY_NAME_KEY_MAX_LENGTH, collation="C"), nullable=False
    )
    created_by: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    @classmethod
    def from_domain(cls, company: Company) -> Self:
        """Derive the comparison key from the validated display name before staging."""

        return cls(
            id=company.id,
            workspace_id=company.workspace_id,
            name=company.name,
            ticker=company.ticker,
            normalized_name=company.normalized_name,
            created_by=company.created_by,
            created_at=company.created_at,
        )

    def to_domain(self) -> Company:
        """Reject inconsistent stored name data rather than silently accepting it."""

        company = Company(
            id=self.id,
            workspace_id=self.workspace_id,
            name=self.name,
            ticker=self.ticker,
            created_by=self.created_by,
            created_at=self.created_at,
        )
        if self.name != company.name or self.normalized_name != company.normalized_name:
            raise CompanyNameIntegrityError("stored company name is inconsistent")
        return company


class SqlAlchemyCompanyRepository:
    """Use one caller-owned session without committing or accessing other module tables."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, context: WorkspaceContext, company: Company) -> None:
        """Validate trusted ownership before staging; the database arbitrates duplicates."""

        if company.workspace_id != context.workspace_id or company.created_by != context.user_id:
            raise CompanyScopeError("company write is outside the trusted context")
        self._session.add(CompanyRow.from_domain(company))

    def get_by_id(self, context: WorkspaceContext, company_id: UUID) -> Company | None:
        """Never resolve another workspace's company from its UUID alone."""

        row = self._session.scalar(
            select(CompanyRow).where(
                CompanyRow.workspace_id == context.workspace_id,
                CompanyRow.id == company_id,
            )
        )
        return None if row is None else row.to_domain()

    def get_by_name(self, context: WorkspaceContext, name: CompanyName) -> Company | None:
        """Find an exact comparison-key match without cross-workspace equivalence."""

        row = self._session.scalar(
            select(CompanyRow).where(
                CompanyRow.workspace_id == context.workspace_id,
                CompanyRow.normalized_name == company_name_key(name),
            )
        )
        return None if row is None else row.to_domain()

    def list_companies(self, context: WorkspaceContext, query: CompanyListQuery) -> CompanyListPage:
        """Filter before limiting; resolve continuation names only from scoped canonical rows."""

        fingerprint = query.fingerprint(context.workspace_id)
        term = query.normalized_query
        anchor = None
        if query.after is not None:
            if query.after.query_fingerprint != fingerprint:
                raise InvalidCompanyCursorError("company cursor is invalid")
            anchor = self.get_by_id(context, query.after.company_id)
            if anchor is None or (
                term is not None
                and term not in anchor.normalized_name
                and term not in (anchor.ticker or "").lower()
            ):
                raise InvalidCompanyCursorError("company cursor is invalid")

        statement = (
            select(CompanyRow)
            .where(CompanyRow.workspace_id == context.workspace_id)
            .order_by(CompanyRow.normalized_name.asc(), CompanyRow.id.asc())
            .limit(query.page_size + 1)
        )
        if term is not None:
            statement = statement.where(
                or_(
                    CompanyRow.normalized_name.contains(term, autoescape=True),
                    func.lower(CompanyRow.ticker).contains(term, autoescape=True),
                )
            )
        if anchor is not None:
            statement = statement.where(
                tuple_(CompanyRow.normalized_name, CompanyRow.id)
                > (anchor.normalized_name, anchor.id)
            )

        rows = self._session.scalars(statement).all()
        companies = tuple(row.to_domain() for row in rows[: query.page_size])
        items = tuple(CompanySummary.from_company(company) for company in companies)
        cursor = (
            CompanyListCursor(company_id=items[-1].company_id, query_fingerprint=fingerprint)
            if len(rows) > query.page_size
            else None
        )
        return CompanyListPage(items=items, next_cursor=cursor)

    def update_ticker(
        self, context: WorkspaceContext, company_id: UUID, ticker: CompanyTicker | None
    ) -> Company | None:
        row = self._session.scalars(
            update(CompanyRow)
            .where(CompanyRow.workspace_id == context.workspace_id, CompanyRow.id == company_id)
            .values(ticker=ticker)
            .returning(CompanyRow)
        ).one_or_none()
        return None if row is None else row.to_domain()
