"""Prove durable local reranking request limits without live provider calls."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import Engine

from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.infrastructure.database.unit_of_work import (
    create_provider_operation_unit_of_work_factory,
)
from investment_research.modules.operations.application import (
    ProviderBudgetPolicy,
    ProviderRequestQuotaUnavailableError,
    RerankingRequestQuotaPolicy,
    ReserveProviderOperationService,
)
from investment_research.modules.operations.domain import (
    ProviderOperation,
    ProviderOperationErrorCode,
    ProviderOperationKind,
    ProviderOperationState,
)
from investment_research.modules.operations.persistence import (
    SqlAlchemyProviderOperationRepository,
)
from tests.integration.factories import (
    ResearchSource,
    make_research_source,
    persist_research_source,
)

MONTH_START = datetime(2026, 10, 1, tzinfo=UTC)
NEXT_MONTH_START = datetime(2026, 11, 1, tzinfo=UTC)


def make_reranking_operation(
    source: ResearchSource, *, started_at: datetime = MONTH_START
) -> ProviderOperation:
    """Build a synthetic zero-price reservation, not a live pricing policy."""

    return ProviderOperation(
        id=uuid4(),
        workspace_id=source.context.workspace_id,
        correlation_id=uuid4(),
        research_request_id=source.request.id,
        operation=ProviderOperationKind.RERANKING,
        provider="pinecone",
        model="bge-reranker-v2-m3",
        configuration_version="m1-5-pinecone-bge-none-v1",
        state=ProviderOperationState.STARTED,
        reserved_cost_micro_usd=0,
        started_at=started_at,
        trace_id=uuid4().hex,
    )


def terminal_operation(
    operation: ProviderOperation, state: ProviderOperationState
) -> ProviderOperation:
    """Preserve unknown failure usage, including a zero-priced timeout."""

    succeeded = state is ProviderOperationState.SUCCEEDED
    return ProviderOperation.model_validate(
        {
            **operation.model_dump(),
            "state": state,
            "actual_cost_micro_usd": 0,
            "input_units": 1 if succeeded else None,
            "output_units": 0 if succeeded else None,
            "completed_at": operation.started_at + timedelta(milliseconds=1),
            "duration_ms": 1,
            "safe_error_code": None if succeeded else ProviderOperationErrorCode.TIMEOUT,
        }
    )


def test_count_includes_all_states_but_excludes_other_kinds_workspaces_and_months(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    source = make_research_source(label="Reranking request count source")
    foreign = make_research_source(label="Foreign reranking request count source")
    persist_research_source(integration_database_engine, source)
    persist_research_source(integration_database_engine, foreign)
    started = make_reranking_operation(source)
    succeeded = make_reranking_operation(source)
    failed = make_reranking_operation(source)
    changed_configuration = make_reranking_operation(source).model_copy(
        update={"provider": "another-provider", "configuration_version": "other-v1"}
    )
    other_kind = make_reranking_operation(source).model_copy(
        update={"operation": ProviderOperationKind.QUERY_EMBEDDING}
    )
    with transactional_session(session_factory) as session:
        repository = SqlAlchemyProviderOperationRepository(session)
        for operation in (
            started,
            succeeded,
            failed,
            changed_configuration,
            other_kind,
            make_reranking_operation(source, started_at=MONTH_START - timedelta(seconds=1)),
            make_reranking_operation(source, started_at=NEXT_MONTH_START),
        ):
            repository.add(source.context, operation)
        repository.add(foreign.context, make_reranking_operation(foreign))
    with transactional_session(session_factory) as session:
        repository = SqlAlchemyProviderOperationRepository(session)
        assert repository.finalize(
            source.context, terminal_operation(succeeded, ProviderOperationState.SUCCEEDED)
        )
        assert repository.finalize(
            source.context, terminal_operation(failed, ProviderOperationState.FAILED)
        )
    # A new session has no in-memory request counter or cached provider state.
    with transactional_session(session_factory) as session:
        repository = SqlAlchemyProviderOperationRepository(session)
        assert (
            repository.count_operations(
                source.context,
                operation=ProviderOperationKind.RERANKING,
                started_at_from=MONTH_START,
                started_at_before=NEXT_MONTH_START,
            )
            == 4
        )
        assert (
            repository.count_operations(
                foreign.context,
                operation=ProviderOperationKind.RERANKING,
                started_at_from=MONTH_START,
                started_at_before=NEXT_MONTH_START,
            )
            == 1
        )


def test_quota_remains_consumed_after_failure_and_resets_only_at_the_next_utc_month(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    source = make_research_source(label="Durable reranking quota source")
    persist_research_source(integration_database_engine, source)
    unit_of_work_factory = create_provider_operation_unit_of_work_factory(session_factory)
    service = ReserveProviderOperationService(
        unit_of_work_factory=unit_of_work_factory,
        budget_policy=ProviderBudgetPolicy(monthly_limit_micro_usd=0),
        reranking_quota_policy=RerankingRequestQuotaPolicy(1),
    )
    first = make_reranking_operation(source)
    service.reserve(source.context, first)
    with transactional_session(session_factory) as session:
        assert SqlAlchemyProviderOperationRepository(session).finalize(
            source.context, terminal_operation(first, ProviderOperationState.FAILED)
        )
    # Recreating the service does not erase a failed or unknown-usage reservation.
    restarted = ReserveProviderOperationService(
        unit_of_work_factory=unit_of_work_factory,
        budget_policy=ProviderBudgetPolicy(monthly_limit_micro_usd=0),
        reranking_quota_policy=RerankingRequestQuotaPolicy(1),
    )
    rejected = make_reranking_operation(source)
    with pytest.raises(ProviderRequestQuotaUnavailableError):
        restarted.reserve(source.context, rejected)
    next_month = make_reranking_operation(source, started_at=NEXT_MONTH_START)
    assert restarted.reserve(source.context, next_month) == next_month
    with transactional_session(session_factory) as session:
        repository = SqlAlchemyProviderOperationRepository(session)
        assert repository.get_by_id(source.context, rejected.id) is None
        stored = repository.get_by_id(source.context, first.id)
        assert stored is not None
        assert stored.state is ProviderOperationState.FAILED


def test_concurrent_zero_price_reservations_cannot_take_the_same_last_quota_slot(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    source = make_research_source(label="Concurrent reranking quota source")
    persist_research_source(integration_database_engine, source)
    service = ReserveProviderOperationService(
        unit_of_work_factory=create_provider_operation_unit_of_work_factory(session_factory),
        budget_policy=ProviderBudgetPolicy(monthly_limit_micro_usd=0),
        reranking_quota_policy=RerankingRequestQuotaPolicy(2),
    )
    service.reserve(source.context, make_reranking_operation(source))
    ready = Barrier(2)

    def reserve(operation: ProviderOperation) -> bool:
        ready.wait(timeout=5)
        try:
            service.reserve(source.context, operation)
        except ProviderRequestQuotaUnavailableError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        accepted = tuple(
            executor.map(
                reserve, (make_reranking_operation(source), make_reranking_operation(source))
            )
        )
    assert sorted(accepted) == [False, True]
    with transactional_session(session_factory) as session:
        assert (
            SqlAlchemyProviderOperationRepository(session).count_operations(
                source.context,
                operation=ProviderOperationKind.RERANKING,
                started_at_from=MONTH_START,
                started_at_before=NEXT_MONTH_START,
            )
            == 2
        )
