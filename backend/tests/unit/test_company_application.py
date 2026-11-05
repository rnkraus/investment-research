"""Verify trusted company creation, scoped reads, and commit-before-return behavior."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from unittest.mock import create_autospec
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.modules.companies.application import (
    CompanyService,
    CompanyUnavailableError,
)
from investment_research.modules.companies.contracts import CompanySummary, CreateCompanyCommand
from investment_research.modules.companies.ports import (
    CompanyCommitUncertainError,
    CompanyNameConflictError,
    CompanyRepository,
    CompanyUnitOfWork,
)
from investment_research.modules.identity.domain import WorkspaceContext
from tests.unit.test_company_persistence import COMPANY_ID, USER_ID, WORKSPACE_ID, make_company


@dataclass
class RecordingUnitOfWork:
    """Expose a mocked repository and simulate a failure when committing on exit."""

    companies: CompanyRepository
    error: Exception | None = None
    events: list[str] = field(default_factory=list)

    @contextmanager
    def open(self) -> Iterator[CompanyUnitOfWork]:
        self.events.append("entered")
        try:
            yield self
            if self.error is not None:
                raise self.error
            self.events.append("completed")
        finally:
            self.events.append("closed")


def test_create_company_uses_trusted_provenance_and_finishes_transaction() -> None:
    repository = create_autospec(CompanyRepository, instance=True)
    unit = RecordingUnitOfWork(cast(CompanyRepository, repository))
    service = CompanyService(
        write_unit_of_work_factory=unit.open,
        read_unit_of_work_factory=unit.open,
        identifier_factory=lambda: COMPANY_ID,
        clock=lambda: datetime(2026, 11, 2, 12, tzinfo=UTC),
    )
    context = WorkspaceContext(workspace_id=WORKSPACE_ID, user_id=USER_ID)

    result = service.create(context, CreateCompanyCommand(name="  Northstar   Grid "))

    repository.add.assert_called_once_with(context, make_company())
    repository.get_by_name.assert_not_called()
    assert unit.events == ["entered", "completed", "closed"]
    assert result == CompanySummary(
        company_id=COMPANY_ID, name="Northstar Grid", created_at=make_company().created_at
    )
    assert set(result.model_dump()) == {"company_id", "name", "ticker", "created_at"}


@pytest.mark.parametrize("error", [CompanyNameConflictError(), CompanyCommitUncertainError()])
def test_create_company_never_returns_success_or_retries_after_commit_failure(
    error: Exception,
) -> None:
    repository = create_autospec(CompanyRepository, instance=True)
    unit = RecordingUnitOfWork(cast(CompanyRepository, repository), error=error)
    service = CompanyService(
        write_unit_of_work_factory=unit.open, read_unit_of_work_factory=unit.open
    )

    with pytest.raises(type(error)) as caught:
        service.create(
            WorkspaceContext(workspace_id=WORKSPACE_ID, user_id=USER_ID),
            CreateCompanyCommand(name="Northstar Grid"),
        )

    assert caught.value is error
    assert repository.add.call_count == 1
    assert unit.events == ["entered", "closed"]


def test_company_lookup_uses_only_the_scoped_read_transaction() -> None:
    repository = create_autospec(CompanyRepository, instance=True)
    repository.get_by_id.return_value = make_company()
    read = RecordingUnitOfWork(cast(CompanyRepository, repository))
    write = RecordingUnitOfWork(cast(CompanyRepository, repository))
    service = CompanyService(
        write_unit_of_work_factory=write.open, read_unit_of_work_factory=read.open
    )
    context = WorkspaceContext(workspace_id=WORKSPACE_ID, user_id=USER_ID)

    result = service.get_by_id(context, COMPANY_ID)

    repository.get_by_id.assert_called_once_with(context, COMPANY_ID)
    repository.add.assert_not_called()
    assert result.company_id == COMPANY_ID
    assert read.events == ["entered", "completed", "closed"]
    assert write.events == []


def test_company_lookup_reports_absence_without_identity_details() -> None:
    repository = create_autospec(CompanyRepository, instance=True)
    repository.get_by_id.return_value = None
    unit = RecordingUnitOfWork(cast(CompanyRepository, repository))
    service = CompanyService(
        write_unit_of_work_factory=unit.open, read_unit_of_work_factory=unit.open
    )

    with pytest.raises(CompanyUnavailableError, match=r"^company is unavailable$"):
        service.get_by_id(
            WorkspaceContext(workspace_id=WORKSPACE_ID, user_id=USER_ID),
            UUID("10000000-0000-4000-8000-000000000099"),
        )
    assert unit.events[-1] == "closed"


def test_invalid_server_time_fails_before_a_write_transaction() -> None:
    repository = create_autospec(CompanyRepository, instance=True)
    unit = RecordingUnitOfWork(cast(CompanyRepository, repository))
    service = CompanyService(
        write_unit_of_work_factory=unit.open,
        read_unit_of_work_factory=unit.open,
        clock=lambda: datetime(2026, 11, 2),
    )

    with pytest.raises(ValidationError):
        service.create(
            WorkspaceContext(workspace_id=WORKSPACE_ID, user_id=USER_ID),
            CreateCompanyCommand(name="Northstar Grid"),
        )
    assert unit.events == []
    repository.add.assert_not_called()
