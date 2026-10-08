"""Application services for bounded canonical provider-operation accounting."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Final, assert_never
from uuid import UUID, uuid4

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.domain import (
    ProviderOperation,
    ProviderOperationErrorCode,
    ProviderOperationKind,
    ProviderOperationScopeError,
    ProviderOperationState,
    ProviderUsage,
)
from investment_research.modules.operations.ports import (
    OperationSpanName,
    OperationTracer,
    ProviderOperationUnitOfWorkFactory,
)

MAX_MICRO_USD: Final = 9_223_372_036_854_775_807
MICRO_USD_RATE_DENOMINATOR: Final = 1_000_000
MAX_RERANKING_REQUESTS_PER_UTC_MONTH: Final = 500

type OperationClock = Callable[[], datetime]
type MonotonicClock = Callable[[], float]
type OperationIdentifierFactory = Callable[[], UUID]
type TraceIdentifierFactory = Callable[[], str]
type ProviderErrorClassifier = Callable[[Exception], ProviderOperationErrorCode]


def _provider_operation_span_name(operation: ProviderOperationKind) -> OperationSpanName:
    """Map every paid operation to one centrally approved content-safe span name."""

    match operation:
        case ProviderOperationKind.DOCUMENT_EMBEDDING:
            return "embedding.document"
        case ProviderOperationKind.QUERY_EMBEDDING:
            return "embedding.query"
        case ProviderOperationKind.RERANKING:
            return "search.rerank"
        case ProviderOperationKind.ANSWER_GENERATION:
            return "research.generate"
        case ProviderOperationKind.STATEMENT_SUPPORT:
            return "evidence.evaluate_support"
    assert_never(operation)


class ProviderBudgetExceededError(Exception):
    """Prevent an external call whose reservation would exceed the monthly budget."""


class ProviderRequestQuotaUnavailableError(ProviderBudgetExceededError):
    """Prevent reranking when its explicit local request quota is absent or exhausted."""


class ProviderOperationUnavailableError(Exception):
    """Hide whether a provider operation is unknown or belongs to another workspace."""


class ProviderUsageExceededReservationError(RuntimeError):
    """Reject provider usage above its declared per-attempt unit or cost ceilings."""


class ProviderUsageCostExceededReservationError(ProviderUsageExceededReservationError):
    """Reject provider usage whose calculated cost exceeds its committed reservation."""


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
class ProviderCostPolicy:
    """Calculate bounded integer micro-USD cost from provider-reported units."""

    input_micro_usd_per_million_units: int
    output_micro_usd_per_million_units: int

    def __post_init__(self) -> None:
        """Keep both rates non-negative and representable by canonical storage."""

        for rate in (
            self.input_micro_usd_per_million_units,
            self.output_micro_usd_per_million_units,
        ):
            if (
                isinstance(rate, bool)
                or not isinstance(rate, int)
                or not 0 <= rate <= MAX_MICRO_USD
            ):
                raise ValueError("provider rates must be non-negative micro-USD integers")

    def cost_for(self, usage: ProviderUsage) -> int:
        """Round one provider-reported cost upward to a whole micro-US dollar."""

        numerator = (
            usage.input_units * self.input_micro_usd_per_million_units
            + usage.output_units * self.output_micro_usd_per_million_units
        )
        if numerator == 0:
            return 0
        return (numerator + MICRO_USD_RATE_DENOMINATOR - 1) // MICRO_USD_RATE_DENOMINATOR


@dataclass(frozen=True, slots=True)
class RerankingRequestQuotaPolicy:
    """Local workspace safeguard, not proof of remaining provider-account allowance."""

    monthly_request_limit: int

    def __post_init__(self) -> None:
        """Require an explicit experiment ceiling; zero disables new reservations."""

        if (
            isinstance(self.monthly_request_limit, bool)
            or not isinstance(self.monthly_request_limit, int)
            or not 0 <= self.monthly_request_limit <= MAX_RERANKING_REQUESTS_PER_UTC_MONTH
        ):
            raise ValueError("local reranking request quota must be an integer from 0 to 500")


@dataclass(frozen=True, slots=True)
class ProviderOperationAttempt:
    """Content-free identity, scope, and maximum usage for one provider attempt."""

    correlation_id: UUID
    research_request_id: UUID | None
    processing_run_id: UUID | None
    operation: ProviderOperationKind
    provider: str
    model: str
    configuration_version: str
    maximum_usage: ProviderUsage
    cost_policy: ProviderCostPolicy


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
    reranking_quota_policy: RerankingRequestQuotaPolicy | None = None

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
        if (
            operation.operation is ProviderOperationKind.RERANKING
            and self.reranking_quota_policy is None
        ):
            raise ProviderRequestQuotaUnavailableError("local reranking quota is not configured")

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
            if operation.operation is ProviderOperationKind.RERANKING:
                assert self.reranking_quota_policy is not None
                request_count = repository.count_operations(
                    context,
                    operation=ProviderOperationKind.RERANKING,
                    started_at_from=window_start,
                    started_at_before=window_end,
                )
                if request_count >= self.reranking_quota_policy.monthly_request_limit:
                    raise ProviderRequestQuotaUnavailableError("local reranking quota exhausted")
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


@dataclass(frozen=True, slots=True)
class ExecuteProviderOperationService:
    """Reserve, execute, and finalize one paid call without holding a transaction open."""

    reservation_service: ReserveProviderOperationService
    finalization_service: FinalizeProviderOperationService
    tracer: OperationTracer
    clock: OperationClock = lambda: datetime.now(UTC)
    monotonic_clock: MonotonicClock = monotonic
    identifier_factory: OperationIdentifierFactory = uuid4
    trace_identifier_factory: TraceIdentifierFactory = lambda: uuid4().hex

    def execute[ResultT](
        self,
        context: WorkspaceContext,
        attempt: ProviderOperationAttempt,
        provider_call: Callable[[], ResultT],
        usage_from_result: Callable[[ResultT], ProviderUsage],
        classify_error: ProviderErrorClassifier,
    ) -> ResultT:
        """Execute one call between separately committed reservation and finalization writes."""

        span_attributes = {
            "workspace.id": str(context.workspace_id),
            "correlation.id": str(attempt.correlation_id),
            "provider.name": attempt.provider,
            "model.name": attempt.model,
            "configuration.version": attempt.configuration_version,
            "operation.kind": attempt.operation.value,
            **self._relationship_trace_attribute(attempt),
        }
        with self.tracer.start_span(
            _provider_operation_span_name(attempt.operation),
            attributes=span_attributes,
        ) as span:
            trace_id = span.trace_id or self.trace_identifier_factory()
            started_at = self._aware_utc_now()
            reserved_cost = attempt.cost_policy.cost_for(attempt.maximum_usage)
            operation = ProviderOperation(
                id=self.identifier_factory(),
                workspace_id=context.workspace_id,
                correlation_id=attempt.correlation_id,
                research_request_id=attempt.research_request_id,
                processing_run_id=attempt.processing_run_id,
                operation=attempt.operation,
                provider=attempt.provider,
                model=attempt.model,
                configuration_version=attempt.configuration_version,
                state=ProviderOperationState.STARTED,
                reserved_cost_micro_usd=reserved_cost,
                started_at=started_at,
                trace_id=trace_id,
            )
            try:
                self.reservation_service.reserve(context, operation)
            except ProviderBudgetExceededError:
                span.set_attribute("outcome", "budget_rejected")
                span.record_error("budget_exceeded")
                raise

            started_tick = self.monotonic_clock()
            try:
                result = provider_call()
                usage = usage_from_result(result)
                actual_cost = attempt.cost_policy.cost_for(usage)
                if actual_cost > reserved_cost:
                    raise ProviderUsageCostExceededReservationError(
                        "provider usage cost exceeded its reservation"
                    )
                if (
                    usage.input_units > attempt.maximum_usage.input_units
                    or usage.output_units > attempt.maximum_usage.output_units
                ):
                    raise ProviderUsageExceededReservationError(
                        "provider usage units exceeded their declared maximum"
                    )
            except Exception as error:
                error_code = self._safe_error_code(error, classify_error)
                completion = ProviderOperationCompletion(
                    state=ProviderOperationState.FAILED,
                    actual_cost_micro_usd=reserved_cost,
                    input_units=None,
                    output_units=None,
                    completed_at=self._aware_utc_now(),
                    duration_ms=self._duration_ms(started_tick),
                    safe_error_code=error_code,
                )
                self.finalization_service.finalize(context, operation.id, completion)
                span.set_attribute("lifecycle.state", ProviderOperationState.FAILED.value)
                span.set_attribute("cost.micro_usd", reserved_cost)
                span.set_attribute("outcome", "failed")
                span.record_error(error_code.value.lower())
                raise

            completion = ProviderOperationCompletion(
                state=ProviderOperationState.SUCCEEDED,
                actual_cost_micro_usd=actual_cost,
                input_units=usage.input_units,
                output_units=usage.output_units,
                completed_at=self._aware_utc_now(),
                duration_ms=self._duration_ms(started_tick),
            )
            self.finalization_service.finalize(context, operation.id, completion)
            span.set_attribute("lifecycle.state", ProviderOperationState.SUCCEEDED.value)
            span.set_attribute("input.units", usage.input_units)
            span.set_attribute("output.units", usage.output_units)
            span.set_attribute("cost.micro_usd", actual_cost)
            span.set_attribute("duration.ms", completion.duration_ms)
            span.set_attribute("outcome", "succeeded")
            return result

    def _aware_utc_now(self) -> datetime:
        """Require one aware application clock and normalize it to UTC."""

        current = self.clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise RuntimeError("provider operation clock must return an aware datetime")
        return current.astimezone(UTC)

    def _duration_ms(self, started_tick: float) -> int:
        """Return one non-negative monotonic duration in whole milliseconds."""

        elapsed = self.monotonic_clock() - started_tick
        if elapsed < 0:
            raise RuntimeError("provider operation monotonic clock moved backwards")
        return int(elapsed * 1_000)

    @staticmethod
    def _relationship_trace_attribute(
        attempt: ProviderOperationAttempt,
    ) -> dict[str, str]:
        """Expose exactly one safe canonical owner identifier to the trace."""

        if attempt.research_request_id is not None:
            return {"research.request.id": str(attempt.research_request_id)}
        if attempt.processing_run_id is not None:
            return {"processing.run.id": str(attempt.processing_run_id)}
        return {}

    @staticmethod
    def _safe_error_code(
        error: Exception,
        classify_error: ProviderErrorClassifier,
    ) -> ProviderOperationErrorCode:
        """Classify one failure without ever persisting its text."""

        if isinstance(error, ProviderUsageExceededReservationError):
            return ProviderOperationErrorCode.CONTRACT
        try:
            return classify_error(error)
        except Exception:
            return ProviderOperationErrorCode.UNAVAILABLE


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
