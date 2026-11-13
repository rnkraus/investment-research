"""Resolve company mentions against workspace-scoped canonical metadata."""

from collections.abc import Set
from uuid import UUID

from sqlalchemy import or_, select

from investment_research.infrastructure.database.session import SessionFactory
from investment_research.modules.companies.persistence import CompanyRow
from investment_research.modules.documents.persistence import DocumentCompanyRow
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.research.company_priority import company_lookup_terms


class PostgresCompanyPriorityReader:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def preferred_documents(
        self, context: WorkspaceContext, question: str, document_ids: Set[UUID]
    ) -> frozenset[UUID]:
        if not document_ids:
            return frozenset()
        names, tickers = company_lookup_terms(question)
        with self._session_factory() as session:
            matches = (
                session.execute(
                    select(CompanyRow.id, CompanyRow.normalized_name, CompanyRow.ticker)
                    .where(
                        CompanyRow.workspace_id == context.workspace_id,
                        or_(CompanyRow.normalized_name.in_(names), CompanyRow.ticker.in_(tickers)),
                    )
                    .limit(21)
                )
                .tuples()
                .all()
            )
            if len(matches) > 20:
                return frozenset()
            ticker_matches = {ticker.casefold() for _, _, ticker in matches if ticker in tickers}
            company_ids = {
                company_id
                for company_id, name, ticker in matches
                if ticker in tickers or name not in ticker_matches
            }
            if len(company_ids) != 1:
                return frozenset()
            return frozenset(
                session.scalars(
                    select(DocumentCompanyRow.document_id).where(
                        DocumentCompanyRow.workspace_id == context.workspace_id,
                        DocumentCompanyRow.company_id == next(iter(company_ids)),
                        DocumentCompanyRow.document_id.in_(document_ids),
                        DocumentCompanyRow.review_status == "REVIEWED",
                    )
                )
            )
