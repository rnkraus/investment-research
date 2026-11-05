"""Verify scoped company links and their interaction with permanent deletion."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import Engine, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from investment_research.infrastructure.database.document_deletion import (
    PostgresDocumentDeletionRepository,
)
from investment_research.modules.companies.persistence import (
    CompanyRow,
    SqlAlchemyCompanyRepository,
)
from investment_research.modules.documents.deletion_contracts import DeleteDocumentsCommand
from investment_research.modules.documents.persistence import DocumentCompanyRow
from tests.integration.factories import ResearchSource
from tests.integration.test_company_postgresql import make_company
from tests.integration.test_document_deletion_postgresql import PURGE_TIME, persist_unclaimed_source
from tests.integration.test_document_listing_postgresql import add_document


def make_link(source: ResearchSource, company: CompanyRow) -> DocumentCompanyRow:
    return DocumentCompanyRow(
        workspace_id=source.context.workspace_id,
        document_id=source.document.id,
        company_id=company.id,
        reviewed_by=source.context.user_id,
        reviewed_at=PURGE_TIME,
    )


def test_document_company_foreign_key_and_duplicate_link(
    integration_database_engine: Engine,
) -> None:
    engine = integration_database_engine
    owner = persist_unclaimed_source(engine)
    foreign = persist_unclaimed_source(engine)
    own_company = CompanyRow.from_domain(make_company(owner.context))
    foreign_company = CompanyRow.from_domain(make_company(foreign.context))
    with Session(engine, expire_on_commit=False) as session, session.begin():
        session.add_all([own_company, foreign_company])
        session.flush()
        session.add(make_link(owner, own_company))

    for company, sqlstate in ((foreign_company, "23503"), (own_company, "23505")):
        with pytest.raises(IntegrityError) as caught, Session(engine) as session, session.begin():
            session.add(make_link(owner, company))
        assert getattr(caught.value.orig, "sqlstate", None) == sqlstate

    with Session(engine) as session:
        link = session.scalars(
            select(DocumentCompanyRow).where(
                DocumentCompanyRow.workspace_id == owner.context.workspace_id
            )
        ).one()
        assert (link.method, link.review_status, link.reviewed_by) == (
            "MANUAL",
            "REVIEWED",
            owner.context.user_id,
        )


def test_document_company_writes_and_permanent_deletion_serialize(
    integration_database_engine: Engine,
) -> None:
    engine = integration_database_engine
    source = persist_unclaimed_source(engine)
    company = CompanyRow.from_domain(make_company(source.context))
    other_company = CompanyRow.from_domain(make_company(source.context, "Other company"))
    with Session(engine, expire_on_commit=False) as session, session.begin():
        session.add_all([company, other_company])
        retained = add_document(session, source.context)
        session.flush()
        retained_link = make_link(source, company)
        retained_link.document_id = retained.id
        session.add(retained_link)

    command = DeleteDocumentsCommand(document_ids=(source.document.id,))

    def competing_deletion() -> str | None:
        try:
            with Session(engine) as session, session.begin():
                session.execute(text("SET LOCAL lock_timeout = '200ms'"))
                PostgresDocumentDeletionRepository(session).request(
                    source.context, command, PURGE_TIME
                )
        except DBAPIError as error:
            return str(getattr(error.orig, "sqlstate", "unknown"))
        return None

    with ThreadPoolExecutor(max_workers=1) as pool, Session(engine) as session, session.begin():
        session.add(make_link(source, company))
        session.flush()
        assert pool.submit(competing_deletion).result(timeout=5) == "55P03"

    with Session(engine) as session, session.begin():
        PostgresDocumentDeletionRepository(session).request(source.context, command, PURGE_TIME)

    with pytest.raises(IntegrityError) as caught, Session(engine) as session, session.begin():
        session.add(make_link(source, other_company))
    assert getattr(caught.value.orig, "sqlstate", None) == "23514"

    with pytest.raises(IntegrityError) as caught, Session(engine) as session, session.begin():
        session.execute(
            update(DocumentCompanyRow)
            .where(
                DocumentCompanyRow.workspace_id == source.context.workspace_id,
                DocumentCompanyRow.document_id == source.document.id,
            )
            .values(company_id=other_company.id)
        )
    assert getattr(caught.value.orig, "sqlstate", None) == "23514"

    with Session(engine) as session, session.begin():
        PostgresDocumentDeletionRepository(session).complete(
            source.context, source.document.id, PURGE_TIME
        )

    with Session(engine) as session:
        remaining = session.scalars(
            select(DocumentCompanyRow).where(
                DocumentCompanyRow.workspace_id == source.context.workspace_id
            )
        ).one()
        assert (remaining.document_id, remaining.company_id) == (retained.id, company.id)
        assert (
            SqlAlchemyCompanyRepository(session).get_by_id(source.context, company.id) is not None
        )
