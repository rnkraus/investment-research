"""Prove scoped company identity and concurrent uniqueness in disposable PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from psycopg.errors import CheckViolation, ForeignKeyViolation, UniqueViolation
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from investment_research.modules.companies.domain import Company
from investment_research.modules.companies.persistence import (
    CompanyRow,
    SqlAlchemyCompanyRepository,
)
from investment_research.modules.identity.domain import WorkspaceContext
from tests.integration.test_document_capture_postgresql import create_workspace


def make_company(context: WorkspaceContext, name: str = "Northstar Grid") -> Company:
    """Build a new identity without assigning another workspace or creator."""

    return Company(
        id=uuid4(),
        workspace_id=context.workspace_id,
        name=name,
        created_by=context.user_id,
        created_at=datetime(2026, 11, 2, 12, tzinfo=UTC),
    )


def test_company_round_trip_hides_foreign_and_unknown_identity(
    integration_database_engine: Engine,
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    other = create_workspace(engine)
    company = make_company(context, " Cafe\u0301   Research ")
    with Session(engine) as session, session.begin():
        SqlAlchemyCompanyRepository(session).add(context, company)

    with Session(engine) as session:
        repository = SqlAlchemyCompanyRepository(session)
        assert repository.get_by_id(context, company.id) == company
        assert repository.get_by_name(context, "CAFÉ RESEARCH") == company
        assert repository.get_by_id(other, company.id) is None
        assert repository.get_by_name(other, company.name) is None
        assert repository.get_by_id(context, uuid4()) is None
        assert repository.get_by_name(context, "Unknown Company") is None


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Northstar Grid", " NORTHSTAR   GRID "),
        ("Cafe\u0301 Research", "CAFÉ RESEARCH"),
        ("Straße AG", "STRASSE AG"),
    ],
)
def test_company_name_uniqueness_is_enforced_by_postgresql(
    integration_database_engine: Engine, first: str, second: str
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    company = make_company(context, first)
    with Session(engine) as session, session.begin():
        SqlAlchemyCompanyRepository(session).add(context, company)

    with pytest.raises(IntegrityError) as error, Session(engine) as session, session.begin():
        SqlAlchemyCompanyRepository(session).add(context, make_company(context, second))

    assert isinstance(error.value.orig, UniqueViolation)
    assert error.value.orig.diag.constraint_name == "uq_companies_workspace_id_normalized_name"
    with Session(engine) as session:
        assert SqlAlchemyCompanyRepository(session).get_by_name(context, second) == company


def test_the_same_company_name_is_allowed_in_different_workspaces(
    integration_database_engine: Engine,
) -> None:
    engine = integration_database_engine
    contexts = (create_workspace(engine), create_workspace(engine))
    companies = tuple(make_company(context) for context in contexts)
    with Session(engine) as session, session.begin():
        repository = SqlAlchemyCompanyRepository(session)
        for context, company in zip(contexts, companies, strict=True):
            repository.add(context, company)
    with Session(engine) as session:
        repository = SqlAlchemyCompanyRepository(session)
        for context, company in zip(contexts, companies, strict=True):
            assert repository.get_by_name(context, "NORTHSTAR GRID") == company


def test_company_uniqueness_does_not_strip_accents_or_punctuation(
    integration_database_engine: Engine,
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    companies = tuple(
        make_company(context, name) for name in ("Cafe Research", "Café Research", "A-B", "AB")
    )
    with Session(engine) as session, session.begin():
        for company in companies:
            SqlAlchemyCompanyRepository(session).add(context, company)
    with Session(engine) as session:
        repository = SqlAlchemyCompanyRepository(session)
        assert tuple(repository.get_by_name(context, company.name) for company in companies) == (
            companies
        )


def test_company_write_rolls_back_with_the_caller_transaction(
    integration_database_engine: Engine,
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    company = make_company(context)
    with (
        pytest.raises(RuntimeError, match="synthetic company rollback"),
        Session(engine) as session,
        session.begin(),
    ):
        SqlAlchemyCompanyRepository(session).add(context, company)
        session.flush()
        raise RuntimeError("synthetic company rollback")
    with Session(engine) as session:
        assert SqlAlchemyCompanyRepository(session).get_by_id(context, company.id) is None


def test_company_requires_an_existing_workspace_foreign_key(
    integration_database_engine: Engine,
) -> None:
    context = WorkspaceContext(workspace_id=uuid4(), user_id=uuid4())
    with (
        pytest.raises(IntegrityError) as error,
        Session(integration_database_engine) as session,
        session.begin(),
    ):
        SqlAlchemyCompanyRepository(session).add(context, make_company(context))

    assert isinstance(error.value.orig, ForeignKeyViolation)


@pytest.mark.parametrize("field", ["name", "normalized_name"])
def test_company_rejects_empty_names_at_the_database_boundary(
    integration_database_engine: Engine, field: str
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    row = CompanyRow.from_domain(make_company(context))
    if field == "name":
        row.name = ""
    else:
        row.normalized_name = ""
    with pytest.raises(IntegrityError) as error, Session(engine) as session, session.begin():
        session.add(row)

    assert isinstance(error.value.orig, CheckViolation)


def test_maximum_casefold_expansion_can_be_stored_and_queried(
    integration_database_engine: Engine,
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    company = make_company(context, "\u0390" * 200)
    with Session(engine) as session, session.begin():
        SqlAlchemyCompanyRepository(session).add(context, company)
    with Session(engine) as session:
        assert SqlAlchemyCompanyRepository(session).get_by_name(context, company.name) == company


def test_competing_company_creations_commit_exactly_one_identity(
    integration_database_engine: Engine,
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    barrier = Barrier(2)

    def create(name: str) -> bool:
        company = make_company(context, name)
        barrier.wait(timeout=10)
        try:
            with Session(engine) as session, session.begin():
                SqlAlchemyCompanyRepository(session).add(context, company)
            return True
        except IntegrityError as error:
            assert isinstance(error.orig, UniqueViolation)
            assert error.orig.diag.constraint_name == "uq_companies_workspace_id_normalized_name"
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create, name) for name in ("Straße AG", "STRASSE AG")]
        assert sorted(future.result(timeout=15) for future in futures) == [False, True]
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(CompanyRow)
                .where(CompanyRow.workspace_id == context.workspace_id)
            )
            == 1
        )
