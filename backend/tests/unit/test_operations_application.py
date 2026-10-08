"""Unit tests for canonical provider-operation accounting services."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import cast
from unittest.mock import Mock, call, create_autospec
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.application import (
    ExecuteProviderOperationService,
    FinalizeProviderOperationService,
    ProviderBudgetExceededError,
    ProviderBudgetPolicy,
    ProviderCostPolicy,
    ProviderOperationAttempt,
    ProviderOperationCompletion,
    ProviderOperationUnavailableError,
    ProviderRequestQuotaUnavailableError,
    ProviderUsageCostExceededReservationError,
    ProviderUsageExceededReservationError,
    RerankingRequestQuotaPolicy,
    ReserveProviderOperationService,
)
from investment_research.modules.operations.domain import (
    ProviderCostSummary,
    ProviderOperation,
    ProviderOperationErrorCode,
    ProviderOperationKind,
    ProviderOperationScopeError,
    ProviderOperationState,
    ProviderUsage,
)
from investment_research.modules.operations.ports import (
    OperationSpan,
    OperationTracer,
    ProviderOperationRepository,
    ProviderOperationUnitOfWork,
    ProviderOperationUnitOfWorkFactory,
    TraceAttributes,
    TraceAttributeValue,
)

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000101")
FOREIGN_WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000202")
USER_ID = UUID("00000000-0000-0000-0000-000000000303")
OPERATION_ID = UUID("00000000-0000-0000-0000-000000000404")
REQUEST_ID = UUID("00000000-0000-0000-0000-000000000505")
CORRELATION_ID = UUID("00000000-0000-0000-0000-000000000606")
STARTED_AT = datetime(2026, 9, 20, 12, 30, tzinfo=UTC)
COMPLETED_AT = STARTED_AT + timedelta(milliseconds=125)
TRACE_ID = "2" * 32


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


class RecordingSpan:
    """Record safe attributes and categories emitted by the accounting service."""

    def __init__(self, trace_id: str | None = TRACE_ID) -> None:
        self._trace_id = trace_id
        self.attributes: dict[str, TraceAttributeValue] = {}
        self.error_types: list[str] = []

    @property
    def trace_id(self) -> str | None:
        return self._trace_id

    def set_attribute(self, name: str, value: TraceAttributeValue) -> None:
        self.attributes[name] = value

    def record_error(self, error_type: str) -> None:
        self.error_types.append(error_type)


class RecordingTracer:
    """Return one recording span without an SDK dependency in application tests."""

    def __init__(self, span: RecordingSpan | None = None) -> None:
        self.span = span or RecordingSpan()
        self.names: list[str] = []
        self.initial_attributes: list[TraceAttributes | None] = []

    @contextmanager
    def start_span(
        self,
        name: str,
        *,
        attributes: TraceAttributes | None = None,
    ) -> Iterator[OperationSpan]:
        self.names.append(name)
        self.initial_attributes.append(attributes)
        yield self.span


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


def make_query_embedding_attempt(
    *,
    maximum_input_units: int = 128_000,
) -> ProviderOperationAttempt:
    """Build one content-free query-embedding attempt under the M1 price."""

    return ProviderOperationAttempt(
        correlation_id=CORRELATION_ID,
        research_request_id=REQUEST_ID,
        processing_run_id=None,
        operation=ProviderOperationKind.QUERY_EMBEDDING,
        provider="openai",
        model="text-embedding-3-small",
        configuration_version="openai-text-embedding-3-small-1536-v1",
        maximum_usage=ProviderUsage(input_units=maximum_input_units, output_units=0),
        cost_policy=ProviderCostPolicy(
            input_micro_usd_per_million_units=20_000,
            output_micro_usd_per_million_units=0,
        ),
    )


def make_execute_service(
    *,
    span: RecordingSpan | None = None,
) -> tuple[ExecuteProviderOperationService, Mock, Mock, RecordingTracer]:
    """Build one deterministic executor with separately mocked writes."""

    reservation = create_autospec(ReserveProviderOperationService, instance=True)
    finalization = create_autospec(FinalizeProviderOperationService, instance=True)
    tracer = RecordingTracer(span)
    clock_values = iter((STARTED_AT, COMPLETED_AT))
    monotonic_values = iter((10.0, 10.125))
    service = ExecuteProviderOperationService(
        reservation_service=reservation,
        finalization_service=finalization,
        tracer=cast(OperationTracer, tracer),
        clock=lambda: next(clock_values),
        monotonic_clock=lambda: next(monotonic_values),
        identifier_factory=lambda: OPERATION_ID,
        trace_identifier_factory=lambda: "3" * 32,
    )
    return service, reservation, finalization, tracer


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


@pytest.mark.parametrize("limit", [0, 1, 500])
def test_reranking_quota_accepts_explicit_bounded_request_limits(limit: int) -> None:
    assert RerankingRequestQuotaPolicy(limit).monthly_request_limit == limit


@pytest.mark.parametrize("limit", [-1, 501, True, 1.5, "50"])
def test_reranking_quota_rejects_invalid_or_unapproved_limits(limit: object) -> None:
    with pytest.raises(ValueError, match="integer from 0 to 500"):
        RerankingRequestQuotaPolicy(cast(int, limit))


def make_reranking_reservation() -> ProviderOperation:
    """Build a synthetic zero-priced reranking reservation, without a live price claim."""

    return ProviderOperation.model_validate(
        {
            **make_started_operation(reserved_cost_micro_usd=0).model_dump(),
            "operation": ProviderOperationKind.RERANKING,
            "provider": "pinecone",
            "model": "bge-reranker-v2-m3",
            "configuration_version": "m1-5-pinecone-bge-none-v1",
        }
    )


def test_reranking_requires_an_explicit_quota_before_opening_a_transaction() -> None:
    unit_of_work = RecordingUnitOfWork()

    with pytest.raises(ProviderRequestQuotaUnavailableError, match="not configured"):
        make_reservation_service(unit_of_work).reserve(make_context(), make_reranking_reservation())
    assert unit_of_work.enter_count == 0
    unit_of_work.repository_mock.add.assert_not_called()


def test_reranking_checks_cost_and_request_count_under_the_same_transaction_lock() -> None:
    unit_of_work = RecordingUnitOfWork()
    unit_of_work.repository_mock.summarize_cost.return_value = ProviderCostSummary(
        actual_cost_micro_usd=0, active_reserved_cost_micro_usd=0
    )
    unit_of_work.repository_mock.count_operations.return_value = 1
    service = replace(
        make_reservation_service(unit_of_work),
        reranking_quota_policy=RerankingRequestQuotaPolicy(2),
    )
    operation = make_reranking_reservation()
    assert service.reserve(make_context(), operation) is operation
    assert unit_of_work.enter_count == 1
    assert unit_of_work.repository_mock.method_calls == [
        call.acquire_budget_lock(make_context()),
        call.summarize_cost(
            make_context(),
            started_at_from=datetime(2026, 9, 1, tzinfo=UTC),
            started_at_before=datetime(2026, 10, 1, tzinfo=UTC),
        ),
        call.count_operations(
            make_context(),
            operation=ProviderOperationKind.RERANKING,
            started_at_from=datetime(2026, 9, 1, tzinfo=UTC),
            started_at_before=datetime(2026, 10, 1, tzinfo=UTC),
        ),
        call.add(make_context(), operation),
    ]


@pytest.mark.parametrize(("limit", "count"), [(0, 0), (1, 1), (1, 2)])
def test_reranking_denies_disabled_or_exhausted_quota_before_provider_call(
    limit: int, count: int
) -> None:
    unit_of_work = RecordingUnitOfWork()
    unit_of_work.repository_mock.summarize_cost.return_value = ProviderCostSummary(
        actual_cost_micro_usd=0, active_reserved_cost_micro_usd=0
    )
    unit_of_work.repository_mock.count_operations.return_value = count
    executor, _reservation, finalization, _tracer = make_execute_service()
    executor = replace(
        executor,
        reservation_service=replace(
            make_reservation_service(unit_of_work),
            reranking_quota_policy=RerankingRequestQuotaPolicy(limit),
        ),
    )
    attempt = replace(
        make_query_embedding_attempt(maximum_input_units=1),
        operation=ProviderOperationKind.RERANKING,
        provider="pinecone",
        model="bge-reranker-v2-m3",
        configuration_version="m1-5-pinecone-bge-none-v1",
        cost_policy=ProviderCostPolicy(0, 0),
    )
    provider_call = Mock(return_value=ProviderUsage(input_units=1, output_units=0))
    with pytest.raises(ProviderRequestQuotaUnavailableError, match="exhausted"):
        executor.execute(
            make_context(),
            attempt,
            provider_call,
            lambda usage: usage,
            lambda _error: ProviderOperationErrorCode.UNAVAILABLE,
        )
    provider_call.assert_not_called()
    finalization.finalize.assert_not_called()
    unit_of_work.repository_mock.add.assert_not_called()


def test_reranking_quota_does_not_replace_the_combined_dollar_budget() -> None:
    unit_of_work = RecordingUnitOfWork()
    unit_of_work.repository_mock.summarize_cost.return_value = ProviderCostSummary(
        actual_cost_micro_usd=100, active_reserved_cost_micro_usd=0
    )
    service = replace(
        make_reservation_service(unit_of_work),
        reranking_quota_policy=RerankingRequestQuotaPolicy(10),
    )
    operation = make_reranking_reservation().model_copy(update={"reserved_cost_micro_usd": 1})
    with pytest.raises(ProviderBudgetExceededError, match="budget exceeded"):
        service.reserve(make_context(), operation)
    unit_of_work.repository_mock.count_operations.assert_not_called()
    unit_of_work.repository_mock.add.assert_not_called()


def test_disabled_reranking_quota_does_not_block_other_operation_kinds() -> None:
    unit_of_work = RecordingUnitOfWork()
    unit_of_work.repository_mock.summarize_cost.return_value = ProviderCostSummary(
        actual_cost_micro_usd=0, active_reserved_cost_micro_usd=0
    )
    service = replace(
        make_reservation_service(unit_of_work),
        reranking_quota_policy=RerankingRequestQuotaPolicy(0),
    )
    operation = make_started_operation()
    assert service.reserve(make_context(), operation) is operation
    unit_of_work.repository_mock.count_operations.assert_not_called()


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


def test_cost_policy_rounds_fractional_micro_usd_upward() -> None:
    """Never undercount a nonzero provider charge smaller than one micro-US dollar."""

    policy = ProviderCostPolicy(
        input_micro_usd_per_million_units=20_000,
        output_micro_usd_per_million_units=0,
    )

    assert policy.cost_for(ProviderUsage(input_units=1, output_units=0)) == 1
    assert policy.cost_for(ProviderUsage(input_units=50, output_units=0)) == 1
    assert policy.cost_for(ProviderUsage(input_units=51, output_units=0)) == 2
    assert policy.cost_for(ProviderUsage(input_units=0, output_units=0)) == 0


@pytest.mark.parametrize("invalid_rate", [-1, True, 9_223_372_036_854_775_808])
def test_cost_policy_rejects_invalid_rates(invalid_rate: object) -> None:
    """Keep pricing arithmetic inside the canonical integer range."""

    with pytest.raises(ValueError, match="non-negative micro-USD integers"):
        ProviderCostPolicy(
            input_micro_usd_per_million_units=cast(int, invalid_rate),
            output_micro_usd_per_million_units=0,
        )


def test_execute_provider_operation_reserves_calls_and_finalizes_success() -> None:
    """Persist one reservation before the call and actual reported usage after it."""

    service, reservation, finalization, tracer = make_execute_service()
    usage = ProviderUsage(input_units=80, output_units=0)
    provider_call = Mock(return_value=usage)

    result = service.execute(
        make_context(),
        make_query_embedding_attempt(),
        provider_call,
        lambda returned: returned,
        lambda _error: ProviderOperationErrorCode.UNAVAILABLE,
    )

    assert result is usage
    provider_call.assert_called_once_with()
    reserved = reservation.reserve.call_args.args[1]
    assert reserved.id == OPERATION_ID
    assert reserved.trace_id == TRACE_ID
    assert reserved.reserved_cost_micro_usd == 2_560
    assert reserved.state is ProviderOperationState.STARTED
    finalization.finalize.assert_called_once_with(
        make_context(),
        OPERATION_ID,
        ProviderOperationCompletion(
            state=ProviderOperationState.SUCCEEDED,
            actual_cost_micro_usd=2,
            input_units=80,
            output_units=0,
            completed_at=COMPLETED_AT,
            duration_ms=125,
        ),
    )
    assert tracer.names == ["embedding.query"]
    assert tracer.span.attributes == {
        "lifecycle.state": "SUCCEEDED",
        "input.units": 80,
        "output.units": 0,
        "cost.micro_usd": 2,
        "duration.ms": 125,
        "outcome": "succeeded",
    }


@pytest.mark.parametrize(
    ("operation", "research_request_id", "processing_run_id", "expected_span_name"),
    [
        pytest.param(
            ProviderOperationKind.DOCUMENT_EMBEDDING,
            None,
            REQUEST_ID,
            "embedding.document",
            id="document-embedding",
        ),
        pytest.param(
            ProviderOperationKind.QUERY_EMBEDDING,
            REQUEST_ID,
            None,
            "embedding.query",
            id="query-embedding",
        ),
        pytest.param(
            ProviderOperationKind.ANSWER_GENERATION,
            REQUEST_ID,
            None,
            "research.generate",
            id="answer-generation",
        ),
        pytest.param(
            ProviderOperationKind.STATEMENT_SUPPORT,
            REQUEST_ID,
            None,
            "evidence.evaluate_support",
            id="statement-support",
        ),
        pytest.param(
            ProviderOperationKind.RERANKING,
            REQUEST_ID,
            None,
            "search.rerank",
            id="reranking",
        ),
    ],
)
def test_execute_provider_operation_derives_approved_span_name(
    operation: ProviderOperationKind,
    research_request_id: UUID | None,
    processing_run_id: UUID | None,
    expected_span_name: str,
) -> None:
    """Map every paid operation to its canonical content-free tracing span."""

    service, _, _, tracer = make_execute_service()
    attempt = ProviderOperationAttempt(
        correlation_id=CORRELATION_ID,
        research_request_id=research_request_id,
        processing_run_id=processing_run_id,
        operation=operation,
        provider="openai",
        model="synthetic-model",
        configuration_version="synthetic-config-v1",
        maximum_usage=ProviderUsage(input_units=1, output_units=0),
        cost_policy=ProviderCostPolicy(
            input_micro_usd_per_million_units=20_000,
            output_micro_usd_per_million_units=0,
        ),
    )

    service.execute(
        make_context(),
        attempt,
        lambda: ProviderUsage(input_units=1, output_units=0),
        lambda returned: returned,
        lambda _error: ProviderOperationErrorCode.UNAVAILABLE,
    )

    assert tracer.names == [expected_span_name]


def test_execute_provider_operation_rejects_budget_before_provider_call() -> None:
    """Do not contact the provider when the canonical reservation cannot be committed."""

    service, reservation, finalization, tracer = make_execute_service()
    reservation.reserve.side_effect = ProviderBudgetExceededError(
        "monthly provider budget exceeded"
    )
    provider_call = Mock()

    with pytest.raises(ProviderBudgetExceededError, match="budget exceeded"):
        service.execute(
            make_context(),
            make_query_embedding_attempt(),
            provider_call,
            lambda returned: cast(ProviderUsage, returned),
            lambda _error: ProviderOperationErrorCode.UNAVAILABLE,
        )

    provider_call.assert_not_called()
    finalization.finalize.assert_not_called()
    assert tracer.span.attributes == {"outcome": "budget_rejected"}
    assert tracer.span.error_types == ["budget_exceeded"]


def test_execute_provider_operation_books_full_reservation_on_unknown_failure_usage() -> None:
    """Keep a failed paid attempt inside budget when the provider reports no units."""

    service, _reservation, finalization, tracer = make_execute_service()
    raw_marker = "raw provider error must not enter accounting"
    provider_call = Mock(side_effect=RuntimeError(raw_marker))

    with pytest.raises(RuntimeError, match=raw_marker):
        service.execute(
            make_context(),
            make_query_embedding_attempt(maximum_input_units=100),
            provider_call,
            lambda returned: cast(ProviderUsage, returned),
            lambda _error: ProviderOperationErrorCode.TIMEOUT,
        )

    completion = finalization.finalize.call_args.args[2]
    assert completion == ProviderOperationCompletion(
        state=ProviderOperationState.FAILED,
        actual_cost_micro_usd=2,
        input_units=None,
        output_units=None,
        completed_at=COMPLETED_AT,
        duration_ms=125,
        safe_error_code=ProviderOperationErrorCode.TIMEOUT,
    )
    assert raw_marker not in repr(completion)
    assert raw_marker not in repr(tracer.span.attributes)
    assert tracer.span.error_types == ["timeout"]


def test_execute_provider_operation_uses_fallback_trace_id_when_tracing_is_unavailable() -> None:
    """Keep canonical accounting available when optional tracing produces no context."""

    service, reservation, _finalization, _tracer = make_execute_service(
        span=RecordingSpan(trace_id=None)
    )
    usage = ProviderUsage(input_units=1, output_units=0)

    service.execute(
        make_context(),
        make_query_embedding_attempt(maximum_input_units=1),
        lambda: usage,
        lambda returned: returned,
        lambda _error: ProviderOperationErrorCode.UNAVAILABLE,
    )

    reserved = reservation.reserve.call_args.args[1]
    assert reserved.trace_id == "3" * 32


def test_execute_provider_operation_marks_usage_above_reservation_as_contract_failure() -> None:
    """Reject contradictory provider usage while conservatively booking the reserved maximum."""

    service, _reservation, finalization, tracer = make_execute_service()
    usage = ProviderUsage(input_units=101, output_units=0)

    with pytest.raises(
        ProviderUsageCostExceededReservationError,
        match="exceeded its reservation",
    ):
        service.execute(
            make_context(),
            make_query_embedding_attempt(maximum_input_units=50),
            lambda: usage,
            lambda returned: returned,
            lambda _error: ProviderOperationErrorCode.UNAVAILABLE,
        )

    completion = finalization.finalize.call_args.args[2]
    assert completion.state is ProviderOperationState.FAILED
    assert completion.actual_cost_micro_usd == 1
    assert completion.safe_error_code is ProviderOperationErrorCode.CONTRACT
    assert tracer.span.error_types == ["contract"]


@pytest.mark.parametrize(
    "usage",
    [ProviderUsage(input_units=2, output_units=0), ProviderUsage(input_units=1, output_units=1)],
)
def test_zero_price_does_not_disable_per_attempt_unit_limits(usage: ProviderUsage) -> None:
    service, _reservation, finalization, tracer = make_execute_service()
    attempt = replace(
        make_query_embedding_attempt(maximum_input_units=1),
        operation=ProviderOperationKind.RERANKING,
        provider="pinecone",
        model="bge-reranker-v2-m3",
        configuration_version="m1-5-pinecone-bge-none-v1",
        cost_policy=ProviderCostPolicy(
            input_micro_usd_per_million_units=0,
            output_micro_usd_per_million_units=0,
        ),
    )

    with pytest.raises(ProviderUsageExceededReservationError, match="declared maximum"):
        service.execute(
            make_context(),
            attempt,
            lambda: usage,
            lambda returned: returned,
            lambda _error: ProviderOperationErrorCode.UNAVAILABLE,
        )

    completion = finalization.finalize.call_args.args[2]
    assert completion.state is ProviderOperationState.FAILED
    assert completion.actual_cost_micro_usd == 0
    assert completion.input_units is None
    assert completion.output_units is None
    assert completion.safe_error_code is ProviderOperationErrorCode.CONTRACT
    assert tracer.span.error_types == ["contract"]


def test_unit_limits_are_checked_even_when_rounded_cost_fits() -> None:
    service, _reservation, finalization, _tracer = make_execute_service()
    usage = ProviderUsage(input_units=2, output_units=0)
    attempt = make_query_embedding_attempt(maximum_input_units=1)
    assert attempt.cost_policy.cost_for(attempt.maximum_usage) == attempt.cost_policy.cost_for(
        usage
    )

    with pytest.raises(ProviderUsageExceededReservationError, match="declared maximum"):
        service.execute(
            make_context(),
            attempt,
            lambda: usage,
            lambda returned: returned,
            lambda _error: ProviderOperationErrorCode.UNAVAILABLE,
        )

    assert finalization.finalize.call_args.args[2].safe_error_code is (
        ProviderOperationErrorCode.CONTRACT
    )


def test_reranking_records_zero_cost_usage_after_an_accepted_reservation() -> None:
    service, reservation, finalization, tracer = make_execute_service()
    attempt = replace(
        make_query_embedding_attempt(maximum_input_units=1),
        operation=ProviderOperationKind.RERANKING,
        provider="pinecone",
        model="bge-reranker-v2-m3",
        configuration_version="m1-5-pinecone-bge-none-v1",
        cost_policy=ProviderCostPolicy(
            input_micro_usd_per_million_units=0,
            output_micro_usd_per_million_units=0,
        ),
    )
    usage = ProviderUsage(input_units=1, output_units=0)
    provider_call = Mock(return_value=usage)

    assert (
        service.execute(
            make_context(),
            attempt,
            provider_call,
            lambda returned: returned,
            lambda _error: ProviderOperationErrorCode.UNAVAILABLE,
        )
        is usage
    )

    reserved = reservation.reserve.call_args.args[1]
    assert reserved.operation is ProviderOperationKind.RERANKING
    assert reserved.reserved_cost_micro_usd == 0
    completion = finalization.finalize.call_args.args[2]
    assert completion.state is ProviderOperationState.SUCCEEDED
    assert completion.input_units == 1
    assert completion.output_units == 0
    assert completion.actual_cost_micro_usd == 0
    assert tracer.names == ["search.rerank"]
    provider_call.assert_called_once_with()


def test_reranking_budget_rejection_prevents_provider_call() -> None:
    service, reservation, finalization, _tracer = make_execute_service()
    reservation.reserve.side_effect = ProviderBudgetExceededError(
        "monthly provider budget exceeded"
    )
    attempt = replace(make_query_embedding_attempt(), operation=ProviderOperationKind.RERANKING)
    provider_call = Mock()

    with pytest.raises(ProviderBudgetExceededError):
        service.execute(
            make_context(),
            attempt,
            provider_call,
            lambda returned: cast(ProviderUsage, returned),
            lambda _error: ProviderOperationErrorCode.UNAVAILABLE,
        )

    provider_call.assert_not_called()
    finalization.finalize.assert_not_called()
