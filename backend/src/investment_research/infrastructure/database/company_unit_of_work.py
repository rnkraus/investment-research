"""Short company transactions and narrow PostgreSQL name-conflict translation."""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass

from psycopg.errors import UniqueViolation
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from investment_research.infrastructure.database.session import (
    SessionFactory,
    transactional_session,
)
from investment_research.modules.companies.persistence import SqlAlchemyCompanyRepository
from investment_research.modules.companies.ports import (
    CompanyCommitUncertainError,
    CompanyNameConflictError,
    CompanyRepository,
    CompanyUnitOfWork,
    CompanyUnitOfWorkFactory,
)


@dataclass(frozen=True, slots=True)
class SqlAlchemyCompanyRepositories:
    """Company repository bound to one caller-independent session."""

    companies: CompanyRepository


@contextmanager
def sqlalchemy_company_write_unit_of_work(
    session_factory: SessionFactory,
) -> Iterator[CompanyUnitOfWork]:
    """Commit once; do not mistake unrelated constraints or uncertain commits for duplicates."""

    body_completed = False
    try:
        with transactional_session(session_factory) as session:
            yield SqlAlchemyCompanyRepositories(companies=SqlAlchemyCompanyRepository(session))
            body_completed = True
    except IntegrityError as error:
        if (
            isinstance(error.orig, UniqueViolation)
            and error.orig.diag.constraint_name == "uq_companies_workspace_id_normalized_name"
        ):
            raise CompanyNameConflictError("company name already exists") from None
        raise
    except SQLAlchemyError:
        if body_completed:
            raise CompanyCommitUncertainError("company commit outcome is uncertain") from None
        raise


@contextmanager
def sqlalchemy_company_read_unit_of_work(
    session_factory: SessionFactory,
) -> Iterator[CompanyUnitOfWork]:
    """Close a scoped read session without committing a write transaction."""

    with session_factory() as session:
        yield SqlAlchemyCompanyRepositories(companies=SqlAlchemyCompanyRepository(session))


def create_company_write_unit_of_work_factory(
    session_factory: SessionFactory,
) -> CompanyUnitOfWorkFactory:
    """Bind atomic creation to the process-wide session factory."""

    def create_unit_of_work() -> AbstractContextManager[CompanyUnitOfWork]:
        return sqlalchemy_company_write_unit_of_work(session_factory)

    return create_unit_of_work


def create_company_read_unit_of_work_factory(
    session_factory: SessionFactory,
) -> CompanyUnitOfWorkFactory:
    """Bind company lookup to a short, non-committing read session."""

    def create_unit_of_work() -> AbstractContextManager[CompanyUnitOfWork]:
        return sqlalchemy_company_read_unit_of_work(session_factory)

    return create_unit_of_work
