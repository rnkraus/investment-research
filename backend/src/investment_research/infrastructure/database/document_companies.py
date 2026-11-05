"""PostgreSQL transactions for scoped document company assignments."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, and_, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from investment_research.infrastructure.database.session import (
    SessionFactory,
    transactional_session,
)
from investment_research.modules.companies.contracts import CompanySummary
from investment_research.modules.companies.persistence import CompanyRow
from investment_research.modules.documents.company_assignment import (
    COMPANY_ASSIGNMENT_LIMIT,
    DocumentCompanyAssignment,
    DocumentCompanyRepository,
    DocumentCompanyService,
)
from investment_research.modules.documents.domain import DocumentLifecycleState
from investment_research.modules.documents.persistence import (
    DocumentCompanyRevisionRow,
    DocumentCompanyRow,
    DocumentRow,
)
from investment_research.modules.identity.domain import WorkspaceContext


class PostgresDocumentCompanyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def read(
        self, context: WorkspaceContext, document_id: UUID, *, for_update: bool = False
    ) -> DocumentCompanyAssignment | None:
        # Acquire the document lock before reading the links and revision under READ COMMITTED.
        version_id = self._session.scalar(
            select(DocumentRow.current_version_id)
            .where(
                DocumentRow.workspace_id == context.workspace_id,
                DocumentRow.id == document_id,
                DocumentRow.lifecycle_state == DocumentLifecycleState.ACTIVE,
            )
            .with_for_update(read=not for_update)
        )
        if version_id is None:
            return None

        revision = self._session.get(
            DocumentCompanyRevisionRow, (context.workspace_id, document_id)
        )
        companies = self._summaries(
            select(CompanyRow)
            .join(
                DocumentCompanyRow,
                and_(
                    DocumentCompanyRow.workspace_id == CompanyRow.workspace_id,
                    DocumentCompanyRow.company_id == CompanyRow.id,
                ),
            )
            .where(
                DocumentCompanyRow.workspace_id == context.workspace_id,
                DocumentCompanyRow.document_id == document_id,
            )
            .limit(COMPANY_ASSIGNMENT_LIMIT + 1)
        )
        return DocumentCompanyAssignment(
            document_id=document_id,
            document_version_id=version_id,
            revision=revision.revision if revision is not None else 0,
            companies=companies,
        )

    def find_companies(
        self, context: WorkspaceContext, company_ids: tuple[UUID, ...]
    ) -> tuple[CompanySummary, ...]:
        if not company_ids:
            return ()
        return self._summaries(
            select(CompanyRow).where(
                CompanyRow.workspace_id == context.workspace_id, CompanyRow.id.in_(company_ids)
            )
        )

    def _summaries(self, statement: Select[tuple[CompanyRow]]) -> tuple[CompanySummary, ...]:
        rows = self._session.scalars(statement.order_by(CompanyRow.normalized_name, CompanyRow.id))
        companies = (row.to_domain() for row in rows)
        return tuple(
            CompanySummary(company_id=company.id, name=company.name, created_at=company.created_at)
            for company in companies
        )

    def apply_changes(
        self,
        context: WorkspaceContext,
        document_id: UUID,
        *,
        removed: set[UUID],
        added: set[UUID],
        revision: int,
        reviewed_at: datetime,
    ) -> None:
        """Apply a validated edit while the transaction owns the exclusive document lock."""

        if removed:
            self._session.execute(
                delete(DocumentCompanyRow).where(
                    DocumentCompanyRow.workspace_id == context.workspace_id,
                    DocumentCompanyRow.document_id == document_id,
                    DocumentCompanyRow.company_id.in_(removed),
                )
            )
        self._session.add_all(
            DocumentCompanyRow(
                workspace_id=context.workspace_id,
                document_id=document_id,
                company_id=company_id,
                reviewed_by=context.user_id,
                reviewed_at=reviewed_at,
            )
            for company_id in sorted(added)
        )
        self._session.execute(
            insert(DocumentCompanyRevisionRow)
            .values(workspace_id=context.workspace_id, document_id=document_id, revision=revision)
            .on_conflict_do_update(
                index_elements=[
                    DocumentCompanyRevisionRow.workspace_id,
                    DocumentCompanyRevisionRow.document_id,
                ],
                set_={"revision": revision},
            )
        )


def create_document_company_service(session_factory: SessionFactory) -> DocumentCompanyService:
    @contextmanager
    def transaction() -> Iterator[DocumentCompanyRepository]:
        with transactional_session(session_factory) as session:
            yield PostgresDocumentCompanyRepository(session)

    return DocumentCompanyService(transaction)
