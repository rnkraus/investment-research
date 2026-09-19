"""Unit tests for canonical provider-operation invariants."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.modules.operations.domain import (
    ProviderCostSummary,
    ProviderOperation,
    ProviderOperationErrorCode,
    ProviderOperationKind,
    ProviderOperationState,
)

OPERATION_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
CORRELATION_ID = UUID("00000000-0000-4000-8000-000000000003")
RESEARCH_REQUEST_ID = UUID("00000000-0000-4000-8000-000000000004")
PROCESSING_RUN_ID = UUID("00000000-0000-4000-8000-000000000005")
STARTED_AT = datetime(2026, 1, 1, tzinfo=UTC)
TRACE_ID = "0123456789abcdef0123456789abcdef"


def test_cost_summary_counts_active_reservations_toward_the_budget() -> None:
    summary = ProviderCostSummary(
        actual_cost_micro_usd=120,
        active_reserved_cost_micro_usd=80,
    )

    assert summary.accounted_cost_micro_usd == 200


def operation_values(**overrides: object) -> dict[str, object]:
    """Return one valid STARTED query-embedding record with selected overrides."""

    values: dict[str, object] = {
        "id": OPERATION_ID,
        "workspace_id": WORKSPACE_ID,
        "correlation_id": CORRELATION_ID,
        "research_request_id": RESEARCH_REQUEST_ID,
        "operation": ProviderOperationKind.QUERY_EMBEDDING,
        "provider": "openai",
        "model": "text-embedding-3-small",
        "configuration_version": "m1-query-embedding-v1",
        "state": ProviderOperationState.STARTED,
        "reserved_cost_micro_usd": 20,
        "started_at": STARTED_AT,
        "trace_id": TRACE_ID.upper(),
    }
    values.update(overrides)
    return values


def test_started_operation_is_content_free_and_normalizes_safe_values() -> None:
    operation = ProviderOperation.model_validate(operation_values())

    assert operation.trace_id == TRACE_ID
    assert operation.started_at == STARTED_AT
    assert operation.actual_cost_micro_usd is None
    assert operation.completed_at is None
    assert {
        "question",
        "prompt",
        "document_text",
        "answer_text",
        "credential",
    }.isdisjoint(operation.model_fields_set)


def test_succeeded_operation_accepts_bounded_usage_and_cost() -> None:
    operation = ProviderOperation.model_validate(
        operation_values(
            state=ProviderOperationState.SUCCEEDED,
            actual_cost_micro_usd=12,
            input_units=420,
            output_units=0,
            completed_at=STARTED_AT + timedelta(milliseconds=18),
            duration_ms=18,
        )
    )

    assert operation.state is ProviderOperationState.SUCCEEDED
    assert operation.actual_cost_micro_usd == 12
    assert operation.input_units == 420


def test_failed_operation_requires_only_a_safe_error_category() -> None:
    operation = ProviderOperation.model_validate(
        operation_values(
            state=ProviderOperationState.FAILED,
            actual_cost_micro_usd=0,
            completed_at=STARTED_AT + timedelta(milliseconds=25),
            duration_ms=25,
            safe_error_code=ProviderOperationErrorCode.TIMEOUT,
        )
    )

    assert operation.safe_error_code is ProviderOperationErrorCode.TIMEOUT


@pytest.mark.parametrize(
    ("research_request_id", "processing_run_id"),
    [
        (None, None),
        (RESEARCH_REQUEST_ID, PROCESSING_RUN_ID),
    ],
)
def test_operation_requires_exactly_one_canonical_owner(
    research_request_id: UUID | None,
    processing_run_id: UUID | None,
) -> None:
    with pytest.raises(
        ValidationError,
        match="exactly one of research_request_id or processing_run_id is required",
    ):
        ProviderOperation.model_validate(
            operation_values(
                research_request_id=research_request_id,
                processing_run_id=processing_run_id,
            )
        )


@pytest.mark.parametrize(
    ("operation", "research_request_id", "processing_run_id", "message"),
    [
        (
            ProviderOperationKind.ANSWER_GENERATION,
            None,
            PROCESSING_RUN_ID,
            "research provider operation requires research_request_id",
        ),
        (
            ProviderOperationKind.DOCUMENT_EMBEDDING,
            RESEARCH_REQUEST_ID,
            None,
            "document embedding requires processing_run_id",
        ),
    ],
)
def test_operation_kind_must_match_its_canonical_owner(
    operation: ProviderOperationKind,
    research_request_id: UUID | None,
    processing_run_id: UUID | None,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        ProviderOperation.model_validate(
            operation_values(
                operation=operation,
                research_request_id=research_request_id,
                processing_run_id=processing_run_id,
            )
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"actual_cost_micro_usd": 21},
            "actual cost must not exceed the reserved cost",
        ),
        (
            {"completed_at": STARTED_AT, "duration_ms": 0, "actual_cost_micro_usd": 0},
            "STARTED operation must not contain completion values",
        ),
        (
            {"state": ProviderOperationState.SUCCEEDED},
            "terminal operation requires cost, completion time, and duration",
        ),
        (
            {
                "state": ProviderOperationState.SUCCEEDED,
                "actual_cost_micro_usd": 0,
                "completed_at": STARTED_AT,
                "duration_ms": 0,
                "safe_error_code": ProviderOperationErrorCode.CONTRACT,
            },
            "safe_error_code is allowed only for FAILED operations",
        ),
        (
            {
                "state": ProviderOperationState.FAILED,
                "actual_cost_micro_usd": 0,
                "completed_at": STARTED_AT,
                "duration_ms": 0,
            },
            "FAILED operation requires safe_error_code",
        ),
    ],
)
def test_lifecycle_and_cost_values_must_remain_coherent(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        ProviderOperation.model_validate(operation_values(**overrides))


def test_operation_rejects_invalid_trace_and_raw_content() -> None:
    with pytest.raises(ValidationError, match="trace_id must not be all zeros"):
        ProviderOperation.model_validate(operation_values(trace_id="0" * 32))

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProviderOperation.model_validate(operation_values(prompt="untrusted raw content"))


def test_operation_state_allows_only_one_forward_finalization() -> None:
    assert ProviderOperationState.STARTED.can_transition_to(ProviderOperationState.SUCCEEDED)
    assert ProviderOperationState.STARTED.can_transition_to(ProviderOperationState.FAILED)
    assert not ProviderOperationState.SUCCEEDED.can_transition_to(ProviderOperationState.FAILED)
    assert not ProviderOperationState.FAILED.can_transition_to(ProviderOperationState.SUCCEEDED)
