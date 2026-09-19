"""Unit tests for canonical provider-operation accounting services."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from typing import cast
from unittest.mock import call, create_autospec
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.application import (
    FinalizeProviderOperationService,
    ProviderBudgetExceededError,
    ProviderBudgetPolicy,
    ProviderOperationCompletion,
    ProviderOperationUnavailableError,
    ReserveProviderOperationService,
)
from investment_research.modules.operations.domain import (
    ProviderCostSummary,
    ProviderOperation,
    ProviderOperationErrorCode,
    ProviderOperationKind,
    ProviderOperationScopeError,
    ProviderOperationState,
)
from investment_research.modules.operations.ports import (
    ProviderOperationRepository,
    ProviderOperationUnitOfWork,
    ProviderOperationUnitOfWorkFactory,
)

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000101")
FOREIGN_WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000202")
USER_ID = UUID("00000000-0000-0000-0000-000000000303")
OPERATION_ID = UUID("00000000-0000-0000-0000-000000000404")
REQUEST_ID = UUID("00000000-0000-0000-0000-000000000505")
CORRELATION_ID = UUID("00000000-0000-0000-0000-000000000606")
STARTED_AT = datetime(2026, 9, 20, 12, 30, tzinfo=UTC)
COMPLETED_AT = STARTED_AT + timedelta(milliseconds=125)


class RecordingUnitOfWork:
    """Expose one mocked repository and count opened transaction scopes."""

    def __init__(self) -> None:
        self.repository_mock = create_autospec(ProviderOperationRepository, instance=True)
        self.provider_operations = cast(
            ProviderOperationRepository,
            self.repository_mock,
        )
        self.enter_count = 0

    @contextmanager
    def open(self) -> Iterator[ProviderOperationUnitOfWork]:
        """Yield one logical provider-accounting transaction."""

        self.enter_count += 1
        yield self


def make_context(workspace_id: UUID = WORKSPACE_ID) -> WorkspaceContext:
    """Build one trusted local workspace context."""

    return WorkspaceContext(workspace_id=workspace_id, user_id=USER_ID)


def make_started_operation(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    reserved_cost_micro_usd: int = 30,
    started_at: datetime = STARTED_AT,
) -> ProviderOperation:
    """Build one valid research-owned reservation."""

    return ProviderOperation(
        id=OPERATION_ID,
        workspace_id=workspace_id,
        correlation_id=CORRELATION_ID,
        research_request_id=REQUEST_ID,
        operation=ProviderOperationKind.QUERY_EMBEDDING,
        provider="openai",
        model="text-embedding-3-small",
        configuration_version="openai-text-embedding-3-small-1536-v1",
        state=ProviderOperationState.STARTED,
        reserved_cost_micro_usd=reserved_cost_micro_usd,
        started_at=started_at,
        trace_id="1" * 32,
    )


def make_reservation_service(
    unit_of_work: RecordingUnitOfWork,
    *,
    monthly_limit_micro_usd: int = 100,
) -> ReserveProviderOperationService:
    """Build the reservation service with a typed recording factory."""

    factory: ProviderOperationUnitOfWorkFactory = unit_of_work.open
    return ReserveProviderOperationService(
        unit_of_work_factory=factory,
        budget_policy=ProviderBudgetPolicy(
            monthly_limit_micro_usd=monthly_limit_micro_usd,
        ),
    )


@pytest.mark.parametrize("invalid_limit", [-1, True, 9_223_372_036_854_775_808])
def test_budget_policy_rejects_invalid_database_amounts(invalid_limit: object) -> None:
    """Keep configured budgets inside the canonical integer range."""

    with pytest.raises(ValueError, match="non-negative micro-USD integer"):
        ProviderBudgetPolicy(monthly_limit_micro_usd=cast(int, invalid_limit))


def test_reservation_locks_summarizes_and_adds_inside_one_transaction() -> None:
    """Reserve only after serializing and accounting for existing monthly usage."""

    unit_of_work = RecordingUnitOfWork()
    unit_of_work.repository_mock.summarize_cost.return_value = ProviderCostSummary(
        actual_cost_micro_usd=40,
        active_reserved_cost_micro_usd=30,
    )
    operation = make_started_operation()

    result = make_reservation_service(unit_of_work).reserve(make_context(), operation)

    assert result is operation
    assert unit_of_work.enter_count == 1
    assert unit_of_work.repository_mock.method_calls == [
        call.acquire_budget_lock(make_context()),
        call.summarize_cost(
            make_context(),
            started_at_from=datetime(2026, 9, 1, tzinfo=UTC),
            started_at_before=datetime(2026, 10, 1, tzinfo=UTC),
        ),
        call.add(make_context(), operation),
    ]


def test_reservation_rejects_cost_above_remaining_budget_without_staging() -> None:
    """Prevent the external call when the combined monthly budget would be exceeded."""

    unit_of_work = RecordingUnitOfWork()
    unit_of_work.repository_mock.summarize_cost.return_value = ProviderCostSummary(
        actual_cost_micro_usd=40,
        active_reserved_cost_micro_usd=31,
    )

    with pytest.raises(ProviderBudgetExceededError, match="budget exceeded"):
        make_reservation_service(unit_of_work).reserve(
            make_context(),
            make_started_operation(),
        )

    unit_of_work.repository_mock.add.assert_not_called()


def test_reservation_uses_the_utc_month_containing_the_operation() -> None:
    """Do not let an offset timestamp select a different local calendar month."""

    unit_of_work = RecordingUnitOfWork()
    unit_of_work.repository_mock.summarize_cost.return_value = ProviderCostSummary(
        actual_cost_micro_usd=0,
        active_reserved_cost_micro_usd=0,
    )
    offset_time = datetime(2026, 10, 1, 1, tzinfo=timezone(timedelta(hours=2)))

    make_reservation_service(unit_of_work).reserve(
        make_context(),
        make_started_operation(started_at=offset_time),
    )

    unit_of_work.repository_mock.summarize_cost.assert_called_once_with(
        make_context(),
        started_at_from=datetime(2026, 9, 1, tzinfo=UTC),
        started_at_before=datetime(2026, 10, 1, tzinfo=UTC),
    )


def test_reservation_rejects_foreign_scope_before_opening_transaction() -> None:
    """Fail closed before consulting accounting state for another workspace."""

    unit_of_work = RecordingUnitOfWork()

    with pytest.raises(ProviderOperationScopeError, match="outside"):
        make_reservation_service(unit_of_work).reserve(
            make_context(),
            make_started_operation(workspace_id=FOREIGN_WORKSPACE_ID),
        )

    assert unit_of_work.enter_count == 0


def test_finalize_builds_terminal_record_from_canonical_identity() -> None:
    """Accept measurements while retaining all stored identity and configuration fields."""

    unit_of_work = RecordingUnitOfWork()
    started = make_started_operation()
    unit_of_work.repository_mock.get_by_id.return_value = started
    unit_of_work.repository_mock.finalize.return_value = True
    factory: ProviderOperationUnitOfWorkFactory = unit_of_work.open
    service = FinalizeProviderOperationService(unit_of_work_factory=factory)
    completion = ProviderOperationCompletion(
        state=ProviderOperationState.SUCCEEDED,
        actual_cost_micro_usd=12,
        input_units=80,
        output_units=0,
        completed_at=COMPLETED_AT,
        duration_ms=125,
    )

    result = service.finalize(make_context(), OPERATION_ID, completion)

    assert result.state is ProviderOperationState.SUCCEEDED
    assert result.provider == started.provider
    assert result.model == started.model
    assert result.configuration_version == started.configuration_version
    assert result.actual_cost_micro_usd == 12
    unit_of_work.repository_mock.finalize.assert_called_once_with(make_context(), result)


def test_finalize_records_only_a_safe_failure_category() -> None:
    """Represent a failed call without storing provider messages or response content."""

    unit_of_work = RecordingUnitOfWork()
    unit_of_work.repository_mock.get_by_id.return_value = make_started_operation()
    unit_of_work.repository_mock.finalize.return_value = True
    factory: ProviderOperationUnitOfWorkFactory = unit_of_work.open

    result = FinalizeProviderOperationService(unit_of_work_factory=factory).finalize(
        make_context(),
        OPERATION_ID,
        ProviderOperationCompletion(
            state=ProviderOperationState.FAILED,
            actual_cost_micro_usd=0,
            input_units=None,
            output_units=None,
            completed_at=COMPLETED_AT,
            duration_ms=125,
            safe_error_code=ProviderOperationErrorCode.TIMEOUT,
        ),
    )

    assert result.safe_error_code is ProviderOperationErrorCode.TIMEOUT
    assert {
        "document_text",
        "question",
        "prompt",
        "response_body",
        "provider_error_message",
    }.isdisjoint(result.model_dump())


def test_finalize_hides_unknown_and_foreign_operation_ids() -> None:
    """Use one unavailable result for every inaccessible identifier."""

    unit_of_work = RecordingUnitOfWork()
    unit_of_work.repository_mock.get_by_id.return_value = None
    factory: ProviderOperationUnitOfWorkFactory = unit_of_work.open

    with pytest.raises(ProviderOperationUnavailableError, match="unavailable"):
        FinalizeProviderOperationService(unit_of_work_factory=factory).finalize(
            make_context(),
            OPERATION_ID,
            ProviderOperationCompletion(
                state=ProviderOperationState.SUCCEEDED,
                actual_cost_micro_usd=0,
                input_units=0,
                output_units=0,
                completed_at=COMPLETED_AT,
                duration_ms=125,
            ),
        )

    unit_of_work.repository_mock.finalize.assert_not_called()


def test_completion_rejects_contradictory_state_and_error_code() -> None:
    """Require a safe error code exactly when the provider attempt failed."""

    with pytest.raises(ValueError, match="cannot contain an error code"):
        ProviderOperationCompletion(
            state=ProviderOperationState.SUCCEEDED,
            actual_cost_micro_usd=0,
            input_units=0,
            output_units=0,
            completed_at=COMPLETED_AT,
            duration_ms=125,
            safe_error_code=ProviderOperationErrorCode.CONTRACT,
        )


def test_finalize_rejects_actual_cost_above_the_reservation() -> None:
    """Let the canonical domain model reject measurements outside the reservation."""

    unit_of_work = RecordingUnitOfWork()
    unit_of_work.repository_mock.get_by_id.return_value = make_started_operation(
        reserved_cost_micro_usd=10
    )
    factory: ProviderOperationUnitOfWorkFactory = unit_of_work.open

    with pytest.raises(ValidationError, match="actual cost"):
        FinalizeProviderOperationService(unit_of_work_factory=factory).finalize(
            make_context(),
            OPERATION_ID,
            ProviderOperationCompletion(
                state=ProviderOperationState.SUCCEEDED,
                actual_cost_micro_usd=11,
                input_units=0,
                output_units=0,
                completed_at=COMPLETED_AT,
                duration_ms=125,
            ),
        )

    unit_of_work.repository_mock.finalize.assert_not_called()
