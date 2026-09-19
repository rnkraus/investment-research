"""Application services for bounded canonical provider-operation accounting."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.domain import (
    ProviderOperation,
    ProviderOperationErrorCode,
    ProviderOperationScopeError,
    ProviderOperationState,
)
from investment_research.modules.operations.ports import ProviderOperationUnitOfWorkFactory

MAX_MICRO_USD: Final = 9_223_372_036_854_775_807


class ProviderBudgetExceededError(Exception):
    """Prevent an external call whose reservation would exceed the monthly budget."""


class ProviderOperationUnavailableError(Exception):
    """Hide whether a provider operation is unknown or belongs to another workspace."""


@dataclass(frozen=True, slots=True)
class ProviderBudgetPolicy:
    """Combined workspace budget for all paid provider operations in one UTC month."""

    monthly_limit_micro_usd: int

    def __post_init__(self) -> None:
        """Require an integer amount representable by the canonical database column."""

        if (
            isinstance(self.monthly_limit_micro_usd, bool)
            or not isinstance(self.monthly_limit_micro_usd, int)
            or not 0 <= self.monthly_limit_micro_usd <= MAX_MICRO_USD
        ):
            raise ValueError("monthly provider budget must be a non-negative micro-USD integer")


@dataclass(frozen=True, slots=True)
class ProviderOperationCompletion:
    """Safe terminal measurements reported after one external provider attempt."""

    state: ProviderOperationState
    actual_cost_micro_usd: int
    input_units: int | None
    output_units: int | None
    completed_at: datetime
    duration_ms: int
    safe_error_code: ProviderOperationErrorCode | None = None

    def __post_init__(self) -> None:
        """Reject incomplete or contradictory terminal results before persistence."""

        if self.state not in {
            ProviderOperationState.SUCCEEDED,
            ProviderOperationState.FAILED,
        }:
            raise ValueError("provider operation completion requires a terminal state")
        if self.state is ProviderOperationState.FAILED and self.safe_error_code is None:
            raise ValueError("failed provider operation requires a safe error code")
        if self.state is ProviderOperationState.SUCCEEDED and self.safe_error_code is not None:
            raise ValueError("successful provider operation cannot contain an error code")


@dataclass(frozen=True, slots=True)
class ReserveProviderOperationService:
    """Atomically reserve one paid operation inside a combined monthly budget."""

    unit_of_work_factory: ProviderOperationUnitOfWorkFactory
    budget_policy: ProviderBudgetPolicy

    def reserve(
        self,
        context: WorkspaceContext,
        operation: ProviderOperation,
    ) -> ProviderOperation:
        """Commit STARTED only when existing usage plus this reservation fits."""

        if operation.workspace_id != context.workspace_id:
            raise ProviderOperationScopeError("provider operation is outside the active workspace")
        if operation.state is not ProviderOperationState.STARTED:
            raise ValueError("provider operation reservation requires STARTED state")

        window_start, window_end = _utc_month_window(operation.started_at)
        with self.unit_of_work_factory() as unit_of_work:
            repository = unit_of_work.provider_operations
            repository.acquire_budget_lock(context)
            summary = repository.summarize_cost(
                context,
                started_at_from=window_start,
                started_at_before=window_end,
            )
            remaining_budget = (
                self.budget_policy.monthly_limit_micro_usd - summary.accounted_cost_micro_usd
            )
            if operation.reserved_cost_micro_usd > remaining_budget:
                raise ProviderBudgetExceededError("monthly provider budget exceeded")
            repository.add(context, operation)

        return operation


@dataclass(frozen=True, slots=True)
class FinalizeProviderOperationService:
    """Finalize one reserved operation without accepting mutable identity fields."""

    unit_of_work_factory: ProviderOperationUnitOfWorkFactory

    def finalize(
        self,
        context: WorkspaceContext,
        operation_id: UUID,
        completion: ProviderOperationCompletion,
    ) -> ProviderOperation:
        """Commit one safe terminal result in a separate short transaction."""

        with self.unit_of_work_factory() as unit_of_work:
            repository = unit_of_work.provider_operations
            current = repository.get_by_id(context, operation_id)
            if current is None:
                raise ProviderOperationUnavailableError("provider operation is unavailable")

            terminal = ProviderOperation.model_validate(
                {
                    **current.model_dump(),
                    "state": completion.state,
                    "actual_cost_micro_usd": completion.actual_cost_micro_usd,
                    "input_units": completion.input_units,
                    "output_units": completion.output_units,
                    "completed_at": completion.completed_at,
                    "duration_ms": completion.duration_ms,
                    "safe_error_code": completion.safe_error_code,
                }
            )
            if not repository.finalize(context, terminal):
                raise ProviderOperationUnavailableError("provider operation is unavailable")

        return terminal


def _utc_month_window(moment: datetime) -> tuple[datetime, datetime]:
    """Return the half-open UTC calendar month containing one aware timestamp."""

    if moment.tzinfo is None:
        raise ValueError("provider operation time must be timezone-aware")
    utc_moment = moment.astimezone(UTC)
    start = datetime(utc_moment.year, utc_moment.month, 1, tzinfo=UTC)
    if utc_moment.month == 12:
        end = datetime(utc_moment.year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(utc_moment.year, utc_moment.month + 1, 1, tzinfo=UTC)
    return start, end
