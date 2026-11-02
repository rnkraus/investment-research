"""Check company staging and integrity before any real database access."""

from datetime import UTC, datetime
from unittest.mock import create_autospec
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from investment_research.modules.companies.domain import (
    Company,
    CompanyNameIntegrityError,
    CompanyScopeError,
)
from investment_research.modules.companies.persistence import (
    CompanyRow,
    SqlAlchemyCompanyRepository,
)
from investment_research.modules.companies.ports import CompanyRepository
from investment_research.modules.identity.domain import WorkspaceContext

COMPANY_ID = UUID("10000000-0000-4000-8000-000000000010")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
USER_ID = UUID("00000000-0000-4000-8000-000000000001")
OTHER_ID = UUID("00000000-0000-4000-8000-000000000003")


def make_company() -> Company:
    """Create one validated synthetic company with explicit provenance."""

    return Company(
        id=COMPANY_ID,
        workspace_id=WORKSPACE_ID,
        name="  Northstar   Grid  ",
        created_by=USER_ID,
        created_at=datetime(2026, 11, 2, 12, tzinfo=UTC),
    )


def test_company_repository_stages_without_committing_or_flushing() -> None:
    session = create_autospec(Session, instance=True)
    repository: CompanyRepository = SqlAlchemyCompanyRepository(session)
    company = make_company()

    repository.add(WorkspaceContext(workspace_id=WORKSPACE_ID, user_id=USER_ID), company)

    session.add.assert_called_once()
    row = session.add.call_args.args[0]
    assert isinstance(row, CompanyRow)
    assert row.normalized_name == "northstar grid"
    assert row.to_domain() == company
    session.commit.assert_not_called()
    session.flush.assert_not_called()


@pytest.mark.parametrize(
    ("workspace_id", "user_id"), [(OTHER_ID, USER_ID), (WORKSPACE_ID, OTHER_ID)]
)
def test_company_repository_rejects_untrusted_scope_before_database_access(
    workspace_id: UUID, user_id: UUID
) -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyCompanyRepository(session)

    with pytest.raises(CompanyScopeError, match="outside the trusted context"):
        repository.add(WorkspaceContext(workspace_id=workspace_id, user_id=user_id), make_company())

    assert session.mock_calls == []


@pytest.mark.parametrize("field", ["name", "normalized_name"])
def test_company_row_rejects_inconsistent_stored_names(field: str) -> None:
    row = CompanyRow.from_domain(make_company())
    if field == "name":
        row.name = "  Northstar Grid  "
    else:
        row.normalized_name = "another company"

    with pytest.raises(CompanyNameIntegrityError, match="stored company name is inconsistent"):
        row.to_domain()
