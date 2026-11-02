"""Keep PostgreSQL diagnostics private and classify only the exact company-name conflict."""

from typing import cast
from unittest.mock import create_autospec

import pytest
from psycopg.errors import ForeignKeyViolation, UniqueViolation
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from investment_research.infrastructure.database.company_unit_of_work import (
    create_company_read_unit_of_work_factory,
    create_company_write_unit_of_work_factory,
)
from investment_research.infrastructure.database.session import SessionFactory
from investment_research.modules.companies.ports import (
    CompanyCommitUncertainError,
    CompanyNameConflictError,
)


def test_company_read_closes_without_committing() -> None:
    factory = create_autospec(sessionmaker, instance=True)
    session = create_autospec(Session, instance=True)
    factory.return_value.__enter__.return_value = session
    read = create_company_read_unit_of_work_factory(cast(SessionFactory, factory))

    with read():
        pass

    factory.begin.assert_not_called()
    session.commit.assert_not_called()
    factory.return_value.__exit__.assert_called_once()


@pytest.mark.parametrize(
    ("original_type", "constraint", "is_name_conflict"),
    [
        (UniqueViolation, "uq_companies_workspace_id_normalized_name", True),
        (UniqueViolation, "pk_companies", False),
        (ForeignKeyViolation, "fk_companies_workspace_id_workspaces", False),
    ],
)
def test_only_exact_name_constraint_becomes_a_safe_conflict(
    original_type: type[Exception], constraint: str, is_name_conflict: bool
) -> None:
    original = create_autospec(original_type, instance=True)
    original.diag.constraint_name = constraint
    error = IntegrityError("private statement", {"name": "private name"}, original)
    factory = create_autospec(sessionmaker, instance=True)
    factory.begin.return_value.__exit__.side_effect = error
    write = create_company_write_unit_of_work_factory(cast(SessionFactory, factory))

    with (
        pytest.raises(CompanyNameConflictError if is_name_conflict else IntegrityError) as caught,
        write(),
    ):
        pass

    if is_name_conflict:
        assert str(caught.value) == "company name already exists"
        assert caught.value.__suppress_context__
    else:
        assert caught.value is error


def test_company_commit_disconnect_is_uncertain_and_never_retried() -> None:
    factory = create_autospec(sessionmaker, instance=True)
    factory.begin.return_value.__exit__.side_effect = OperationalError(
        "private statement", {}, Exception("private connection details")
    )
    write = create_company_write_unit_of_work_factory(cast(SessionFactory, factory))

    with pytest.raises(CompanyCommitUncertainError) as caught, write():
        pass

    assert str(caught.value) == "company commit outcome is uncertain"
    assert caught.value.__suppress_context__
    factory.begin.assert_called_once()


def test_company_body_failure_is_not_classified_as_uncertain_commit() -> None:
    factory = create_autospec(sessionmaker, instance=True)
    write = create_company_write_unit_of_work_factory(cast(SessionFactory, factory))
    error = OperationalError("private statement", {}, Exception("private details"))

    with pytest.raises(OperationalError) as caught, write():
        raise error

    assert caught.value is error
