"""PostgreSQL integration tests for canonical provider-operation accounting."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.modules.operations.domain import (
    ProviderOperation,
    ProviderOperationKind,
    ProviderOperationState,
)
from investment_research.modules.operations.persistence import (
    SqlAlchemyProviderOperationRepository,
)
from tests.integration.factories import (
    FIXTURE_TIME,
    ResearchSource,
    make_research_source,
    persist_research_source,
)


def make_started_operation(
    source: ResearchSource,
    *,
    operation_id: UUID,
    correlation_id: UUID,
    operation: ProviderOperationKind,
    reserved_cost_micro_usd: int,
    offset_seconds: int,
    research_request_id: UUID | None = None,
) -> ProviderOperation:
    """Build one valid content-free operation for a persisted research request."""

    return ProviderOperation(
        id=operation_id,
        workspace_id=source.context.workspace_id,
        correlation_id=correlation_id,
        research_request_id=research_request_id or source.request.id,
        operation=operation,
        provider="openai",
        model=(
            "text-embedding-3-small"
            if operation is ProviderOperationKind.QUERY_EMBEDDING
            else "gpt-5.6-terra"
        ),
        configuration_version=f"m1-{operation.value.lower().replace('_', '-')}-v1",
        state=ProviderOperationState.STARTED,
        reserved_cost_micro_usd=reserved_cost_micro_usd,
        started_at=FIXTURE_TIME + timedelta(seconds=offset_seconds),
        trace_id=uuid4().hex,
    )


def succeed_operation(
    operation: ProviderOperation,
    *,
    actual_cost_micro_usd: int,
    input_units: int,
    output_units: int,
) -> ProviderOperation:
    """Return the valid SUCCEEDED snapshot for one started operation."""

    return ProviderOperation.model_validate(
        {
            **operation.model_dump(),
            "state": ProviderOperationState.SUCCEEDED,
            "actual_cost_micro_usd": actual_cost_micro_usd,
            "input_units": input_units,
            "output_units": output_units,
            "completed_at": operation.started_at + timedelta(milliseconds=25),
            "duration_ms": 25,
        }
    )


def test_provider_operations_are_scoped_finalized_and_summarized(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    source = make_research_source(label="Provider Operation Source")
    foreign_source = make_research_source(label="Foreign Provider Operation Source")
    persist_research_source(integration_database_engine, source)
    persist_research_source(integration_database_engine, foreign_source)
    correlation_id = uuid4()
    embedding = make_started_operation(
        source,
        operation_id=uuid4(),
        correlation_id=correlation_id,
        operation=ProviderOperationKind.QUERY_EMBEDDING,
        reserved_cost_micro_usd=20,
        offset_seconds=4,
    )
    generation = make_started_operation(
        source,
        operation_id=uuid4(),
        correlation_id=correlation_id,
        operation=ProviderOperationKind.ANSWER_GENERATION,
        reserved_cost_micro_usd=30,
        offset_seconds=5,
    )

    with transactional_session(session_factory) as session:
        repository = SqlAlchemyProviderOperationRepository(session)
        repository.add(source.context, embedding)
        repository.add(source.context, generation)

    completed_embedding = succeed_operation(
        embedding,
        actual_cost_micro_usd=12,
        input_units=420,
        output_units=0,
    )
    with transactional_session(session_factory) as session:
        repository = SqlAlchemyProviderOperationRepository(session)
        assert repository.finalize(source.context, completed_embedding)

    with transactional_session(session_factory) as session:
        repository = SqlAlchemyProviderOperationRepository(session)
        assert repository.get_by_id(source.context, embedding.id) == completed_embedding
        assert repository.get_by_id(foreign_source.context, embedding.id) is None
        assert repository.list_by_correlation_id(source.context, correlation_id) == (
            completed_embedding,
            generation,
        )
        assert repository.list_by_correlation_id(foreign_source.context, correlation_id) == ()
        summary = repository.summarize_cost(
            source.context,
            started_at_from=FIXTURE_TIME,
            started_at_before=FIXTURE_TIME + timedelta(days=1),
        )

    assert summary.actual_cost_micro_usd == 12
    assert summary.active_reserved_cost_micro_usd == 30
    assert summary.accounted_cost_micro_usd == 42


def test_database_rejects_provider_operation_linked_to_foreign_request(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    source = make_research_source(label="Provider Relationship Source")
    foreign_source = make_research_source(label="Foreign Provider Relationship Source")
    persist_research_source(integration_database_engine, source)
    persist_research_source(integration_database_engine, foreign_source)
    cross_workspace_operation = make_started_operation(
        foreign_source,
        operation_id=uuid4(),
        correlation_id=uuid4(),
        operation=ProviderOperationKind.QUERY_EMBEDDING,
        reserved_cost_micro_usd=20,
        offset_seconds=4,
        research_request_id=source.request.id,
    )

    with (
        pytest.raises(IntegrityError),
        transactional_session(session_factory) as session,
    ):
        SqlAlchemyProviderOperationRepository(session).add(
            foreign_source.context,
            cross_workspace_operation,
        )


def test_provider_operation_uses_the_caller_transaction_boundary(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    source = make_research_source(label="Provider Rollback Source")
    persist_research_source(integration_database_engine, source)
    operation = make_started_operation(
        source,
        operation_id=uuid4(),
        correlation_id=uuid4(),
        operation=ProviderOperationKind.STATEMENT_SUPPORT,
        reserved_cost_micro_usd=10,
        offset_seconds=4,
    )

    with (
        pytest.raises(RuntimeError, match="force rollback"),
        transactional_session(session_factory) as session,
    ):
        SqlAlchemyProviderOperationRepository(session).add(source.context, operation)
        raise RuntimeError("force rollback")

    with transactional_session(session_factory) as session:
        assert (
            SqlAlchemyProviderOperationRepository(session).get_by_id(
                source.context,
                operation.id,
            )
            is None
        )
