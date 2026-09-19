"""Unit tests for scoped provider-operation persistence."""

from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import create_autospec
from uuid import UUID

import pytest
from sqlalchemy import ForeignKeyConstraint, Table
from sqlalchemy.orm import Session

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.domain import (
    InvalidProviderOperationTransitionError,
    ProviderOperation,
    ProviderOperationErrorCode,
    ProviderOperationKind,
    ProviderOperationRelationshipError,
    ProviderOperationScopeError,
    ProviderOperationState,
)
from investment_research.modules.operations.persistence import (
    ProviderOperationRow,
    SqlAlchemyProviderOperationRepository,
)
from investment_research.modules.operations.ports import ProviderOperationRepository

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
OTHER_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000003")
OPERATION_ID = UUID("50000000-0000-4000-8000-000000000001")
OTHER_OPERATION_ID = UUID("50000000-0000-4000-8000-000000000002")
CORRELATION_ID = UUID("50000000-0000-4000-8000-000000000003")
RESEARCH_REQUEST_ID = UUID("50000000-0000-4000-8000-000000000004")
STARTED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)
COMPLETED_AT = STARTED_AT + timedelta(milliseconds=25)
TRACE_ID = "0123456789abcdef0123456789abcdef"


def make_context(workspace_id: UUID = WORKSPACE_ID) -> WorkspaceContext:
    """Build a trusted context for one deterministic workspace."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=workspace_id)


def make_operation(
    state: ProviderOperationState = ProviderOperationState.STARTED,
    *,
    operation_id: UUID = OPERATION_ID,
    workspace_id: UUID = WORKSPACE_ID,
    correlation_id: UUID = CORRELATION_ID,
) -> ProviderOperation:
    """Build one lifecycle-consistent query-embedding operation."""

    terminal = state is not ProviderOperationState.STARTED
    return ProviderOperation(
        id=operation_id,
        workspace_id=workspace_id,
        correlation_id=correlation_id,
        research_request_id=RESEARCH_REQUEST_ID,
        operation=ProviderOperationKind.QUERY_EMBEDDING,
        provider="openai",
        model="text-embedding-3-small",
        configuration_version="m1-query-embedding-v1",
        state=state,
        reserved_cost_micro_usd=20,
        actual_cost_micro_usd=12 if terminal else None,
        input_units=420 if terminal else None,
        output_units=0 if terminal else None,
        started_at=STARTED_AT,
        completed_at=COMPLETED_AT if terminal else None,
        duration_ms=25 if terminal else None,
        trace_id=TRACE_ID,
        safe_error_code=(
            ProviderOperationErrorCode.TIMEOUT if state is ProviderOperationState.FAILED else None
        ),
    )


def test_row_round_trips_started_and_terminal_operations() -> None:
    for operation in (
        make_operation(),
        make_operation(ProviderOperationState.SUCCEEDED),
        make_operation(ProviderOperationState.FAILED),
    ):
        assert ProviderOperationRow.from_domain(operation).to_domain() == operation


def test_repository_matches_application_owned_protocol() -> None:
    session = create_autospec(Session, instance=True)

    repository: ProviderOperationRepository = SqlAlchemyProviderOperationRepository(session)

    assert isinstance(repository, SqlAlchemyProviderOperationRepository)


def test_repository_stages_only_started_operations_in_matching_scope() -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyProviderOperationRepository(session)
    operation = make_operation()

    repository.add(make_context(), operation)

    session.add.assert_called_once()
    row = session.add.call_args.args[0]
    assert isinstance(row, ProviderOperationRow)
    assert row.to_domain() == operation

    with pytest.raises(ProviderOperationScopeError, match="outside the active workspace"):
        repository.add(
            make_context(),
            make_operation(workspace_id=OTHER_WORKSPACE_ID),
        )
    with pytest.raises(ProviderOperationRelationshipError, match="must start in STARTED"):
        repository.add(
            make_context(),
            make_operation(ProviderOperationState.SUCCEEDED),
        )


def test_repository_finalizes_one_started_operation() -> None:
    session = create_autospec(Session, instance=True)
    stored_row = ProviderOperationRow.from_domain(make_operation())
    session.scalar.return_value = stored_row
    repository = SqlAlchemyProviderOperationRepository(session)
    completed = make_operation(ProviderOperationState.SUCCEEDED)

    assert repository.finalize(make_context(), completed) is True
    assert stored_row.to_domain() == completed


def test_repository_rejects_changed_identity_and_second_finalization() -> None:
    session = create_autospec(Session, instance=True)
    stored_row = ProviderOperationRow.from_domain(make_operation())
    session.scalar.return_value = stored_row
    repository = SqlAlchemyProviderOperationRepository(session)
    completed = make_operation(ProviderOperationState.SUCCEEDED)

    changed_model = completed.model_copy(update={"model": "other-model"})
    with pytest.raises(
        ProviderOperationRelationshipError,
        match="identity or configuration changed",
    ):
        repository.finalize(make_context(), changed_model)

    assert repository.finalize(make_context(), completed) is True
    with pytest.raises(
        InvalidProviderOperationTransitionError,
        match="cannot transition",
    ):
        repository.finalize(
            make_context(),
            make_operation(ProviderOperationState.FAILED),
        )


def test_repository_reports_unknown_or_foreign_operations_without_disclosure() -> None:
    session = create_autospec(Session, instance=True)
    session.scalar.return_value = None
    repository = SqlAlchemyProviderOperationRepository(session)

    assert repository.get_by_id(make_context(), OTHER_OPERATION_ID) is None
    assert (
        repository.finalize(
            make_context(),
            make_operation(ProviderOperationState.SUCCEEDED),
        )
        is False
    )

    for statement in session.scalar.call_args_list:
        compiled = str(statement.args[0])
        assert "provider_operations.workspace_id" in compiled


def test_repository_lists_only_the_scoped_correlation_in_stable_order() -> None:
    session = create_autospec(Session, instance=True)
    first = make_operation()
    second = make_operation(
        operation_id=OTHER_OPERATION_ID,
        state=ProviderOperationState.SUCCEEDED,
    )
    session.scalars.return_value = (
        ProviderOperationRow.from_domain(first),
        ProviderOperationRow.from_domain(second),
    )
    repository = SqlAlchemyProviderOperationRepository(session)

    assert repository.list_by_correlation_id(make_context(), CORRELATION_ID) == (
        first,
        second,
    )

    compiled = str(session.scalars.call_args.args[0])
    assert "provider_operations.workspace_id" in compiled
    assert "provider_operations.correlation_id" in compiled
    assert "provider_operations.started_at" in compiled


def test_repository_summarizes_actual_cost_and_active_reservations() -> None:
    session = create_autospec(Session, instance=True)
    session.execute.return_value.one.return_value = (120, 80)
    repository = SqlAlchemyProviderOperationRepository(session)

    summary = repository.summarize_cost(
        make_context(),
        started_at_from=STARTED_AT,
        started_at_before=STARTED_AT + timedelta(days=31),
    )

    assert summary.actual_cost_micro_usd == 120
    assert summary.active_reserved_cost_micro_usd == 80
    assert summary.accounted_cost_micro_usd == 200
    compiled = str(session.execute.call_args.args[0])
    assert "provider_operations.workspace_id" in compiled
    assert "provider_operations.started_at" in compiled


@pytest.mark.parametrize(
    ("started_at_from", "started_at_before", "message"),
    [
        (
            STARTED_AT.replace(tzinfo=None),
            STARTED_AT + timedelta(days=1),
            "must be timezone-aware",
        ),
        (
            STARTED_AT,
            STARTED_AT,
            "must have positive duration",
        ),
    ],
)
def test_cost_summary_rejects_invalid_windows(
    started_at_from: datetime,
    started_at_before: datetime,
    message: str,
) -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyProviderOperationRepository(session)

    with pytest.raises(ValueError, match=message):
        repository.summarize_cost(
            make_context(),
            started_at_from=started_at_from,
            started_at_before=started_at_before,
        )

    session.execute.assert_not_called()


def test_provider_operation_table_enforces_workspace_relationships() -> None:
    table = cast(Table, ProviderOperationRow.__table__)
    targets = {
        tuple(element.target_fullname for element in constraint.elements)
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }

    assert ("workspaces.id",) in targets
    assert ("research_requests.workspace_id", "research_requests.id") in targets
    assert ("processing_runs.workspace_id", "processing_runs.id") in targets
    assert {"workspace_id", "correlation_id", "trace_id"}.issubset(table.c.keys())
