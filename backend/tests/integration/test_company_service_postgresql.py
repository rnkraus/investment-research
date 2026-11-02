"""Exercise committed company use cases through real scoped PostgreSQL transactions."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from psycopg.errors import ForeignKeyViolation, UniqueViolation
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from investment_research.infrastructure.database.company_unit_of_work import (
    create_company_read_unit_of_work_factory,
    create_company_write_unit_of_work_factory,
)
from investment_research.infrastructure.database.session import create_session_factory
from investment_research.modules.companies.application import (
    CompanyService,
    CompanyUnavailableError,
)
from investment_research.modules.companies.contracts import CreateCompanyCommand
from investment_research.modules.companies.persistence import CompanyRow
from investment_research.modules.companies.ports import CompanyNameConflictError
from investment_research.modules.identity.domain import WorkspaceContext
from tests.integration.test_document_capture_postgresql import create_workspace


def company_service(
    engine: Engine, *, identifier_factory: Callable[[], UUID] = uuid4
) -> CompanyService:
    sessions = create_session_factory(engine)
    return CompanyService(
        write_unit_of_work_factory=create_company_write_unit_of_work_factory(sessions),
        read_unit_of_work_factory=create_company_read_unit_of_work_factory(sessions),
        identifier_factory=identifier_factory,
    )


def test_company_service_commits_and_hides_unknown_or_foreign_identities(
    integration_database_engine: Engine,
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    foreign = create_workspace(engine)
    service = company_service(engine)

    company = service.create(context, CreateCompanyCommand(name=" Northstar   Grid "))

    assert company.name == "Northstar Grid"
    assert service.get_by_id(context, company.company_id) == company
    for scope, identifier in ((foreign, company.company_id), (context, uuid4())):
        with pytest.raises(CompanyUnavailableError, match=r"^company is unavailable$"):
            service.get_by_id(scope, identifier)
    with Session(engine) as session:
        row = session.get(CompanyRow, company.company_id)
        assert row is not None
        assert row.workspace_id == context.workspace_id
        assert row.created_by == context.user_id


def test_company_service_reports_name_conflicts_and_keeps_workspace_independence(
    integration_database_engine: Engine,
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    other = create_workspace(engine)
    service = company_service(engine)
    company = service.create(context, CreateCompanyCommand(name="Straße AG"))

    with pytest.raises(CompanyNameConflictError, match=r"^company name already exists$") as caught:
        service.create(context, CreateCompanyCommand(name=" STRASSE   AG "))

    assert caught.value.__suppress_context__
    assert service.get_by_id(context, company.company_id) == company
    independent = service.create(other, CreateCompanyCommand(name="STRASSE AG"))
    assert independent.company_id != company.company_id
    assert service.get_by_id(other, independent.company_id) == independent


def test_company_service_does_not_misclassify_primary_key_collisions(
    integration_database_engine: Engine,
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    identifier = uuid4()
    service = company_service(engine, identifier_factory=lambda: identifier)
    company = service.create(context, CreateCompanyCommand(name="First Company"))

    with pytest.raises(IntegrityError) as caught:
        service.create(context, CreateCompanyCommand(name="Second Company"))

    assert isinstance(caught.value.orig, UniqueViolation)
    assert caught.value.orig.diag.constraint_name == "pk_companies"
    assert service.get_by_id(context, company.company_id) == company


def test_company_service_rejects_missing_workspace_without_committing_a_company(
    integration_database_engine: Engine,
) -> None:
    engine = integration_database_engine
    identifier = uuid4()
    service = company_service(engine, identifier_factory=lambda: identifier)

    with pytest.raises(IntegrityError) as caught:
        service.create(
            WorkspaceContext(workspace_id=uuid4(), user_id=uuid4()),
            CreateCompanyCommand(name="Northstar Grid"),
        )

    assert isinstance(caught.value.orig, ForeignKeyViolation)
    with Session(engine) as session:
        assert session.get(CompanyRow, identifier) is None


def test_competing_company_service_calls_return_one_success_and_one_safe_conflict(
    integration_database_engine: Engine,
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    barrier = Barrier(2)

    def identifier() -> UUID:
        barrier.wait(timeout=10)
        return uuid4()

    service = company_service(engine, identifier_factory=identifier)

    def create(name: str) -> bool:
        try:
            service.create(context, CreateCompanyCommand(name=name))
            return True
        except CompanyNameConflictError as error:
            assert str(error) == "company name already exists"
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create, name) for name in ("Northstar Grid", "NORTHSTAR GRID")]
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
