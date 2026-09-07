"""PostgreSQL integration tests for scoped research persistence."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.modules.research.domain import (
    Answer,
    AnswerState,
    AnswerStatement,
    CoverageStatus,
    StatementSupportStatus,
    StatementType,
)
from investment_research.modules.research.persistence import (
    AnswerStatementRow,
    SqlAlchemyResearchRepository,
)
from tests.integration.factories import (
    FIXTURE_TIME,
    ResearchSource,
    RetrievalResultFixture,
    make_research_source,
    make_retrieval_result,
    persist_research_source,
    persist_retrieval_result,
    persist_workspace,
)


def make_answer(
    source: ResearchSource,
    result: RetrievalResultFixture,
    state: AnswerState,
    *,
    answer_id: UUID,
    research_request_id: UUID | None = None,
    version_number: int = 1,
) -> Answer:
    """Build one lifecycle-consistent snapshot of the same answer version."""

    retrieval_states = {
        AnswerState.GENERATING,
        AnswerState.VALIDATING,
        AnswerState.STREAMED,
        AnswerState.INSUFFICIENT,
        AnswerState.REJECTED,
        AnswerState.REGENERATED,
    }
    terminal_states = {
        AnswerState.STREAMED,
        AnswerState.INSUFFICIENT,
        AnswerState.REJECTED,
        AnswerState.REGENERATED,
    }
    coverage_status = CoverageStatus.COMPLETE if state is AnswerState.STREAMED else None
    rendered_text = source.blocks[0].text if state is AnswerState.STREAMED else None
    return Answer(
        id=answer_id,
        workspace_id=source.context.workspace_id,
        research_request_id=research_request_id or source.request.id,
        retrieval_run_id=result.run.id if state in retrieval_states else None,
        version_number=version_number,
        state=state,
        coverage_status=coverage_status,
        generator_version="fixture-generator-1",
        generation_config_version="generation-m0.0-v1",
        prompt_version="answer-prompt-v1",
        rendered_text=rendered_text,
        created_at=FIXTURE_TIME + timedelta(seconds=6),
        completed_at=(FIXTURE_TIME + timedelta(seconds=7) if state in terminal_states else None),
    )


def make_statements(source: ResearchSource, answer: Answer) -> tuple[AnswerStatement, ...]:
    """Build two accepted archive-derived statements in display order."""

    return tuple(
        AnswerStatement(
            id=uuid4(),
            workspace_id=source.context.workspace_id,
            answer_id=answer.id,
            ordinal=ordinal,
            text=block.text,
            statement_type=StatementType.ARCHIVE_DERIVED,
            support_status=StatementSupportStatus.VALIDATED,
            created_at=answer.completed_at or answer.created_at,
        )
        for ordinal, block in enumerate(source.blocks)
    )


def persist_answer_through_streamed(
    engine: Engine,
    source: ResearchSource,
    result: RetrievalResultFixture,
    *,
    answer_id: UUID,
) -> tuple[Answer, tuple[AnswerStatement, ...]]:
    """Persist every permitted transition and the final ordered statements."""

    session_factory = create_session_factory(engine)
    requested = make_answer(source, result, AnswerState.REQUESTED, answer_id=answer_id)
    with transactional_session(session_factory) as session:
        SqlAlchemyResearchRepository(session).add_answer(source.context, requested)

    for state in (
        AnswerState.RETRIEVING,
        AnswerState.GENERATING,
        AnswerState.VALIDATING,
    ):
        snapshot = make_answer(source, result, state, answer_id=answer_id)
        with transactional_session(session_factory) as session:
            assert SqlAlchemyResearchRepository(session).update_answer(
                source.context,
                snapshot,
            )

    streamed = make_answer(source, result, AnswerState.STREAMED, answer_id=answer_id)
    statements = make_statements(source, streamed)
    with transactional_session(session_factory) as session:
        repository = SqlAlchemyResearchRepository(session)
        assert repository.update_answer(source.context, streamed)
        repository.add_statements(source.context, streamed, statements)
    return streamed, statements


def test_answer_lifecycle_and_statements_are_scoped_and_ordered(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    source = make_research_source(label="Answer Source")
    foreign_source = make_research_source(label="Foreign Answer Source")
    persist_research_source(integration_database_engine, source)
    persist_workspace(integration_database_engine, foreign_source)
    result = make_retrieval_result(source)
    persist_retrieval_result(integration_database_engine, source, result)
    answer_id = uuid4()
    streamed, statements = persist_answer_through_streamed(
        integration_database_engine,
        source,
        result,
        answer_id=answer_id,
    )

    with transactional_session(session_factory) as session:
        repository = SqlAlchemyResearchRepository(session)
        assert repository.get_request_by_id(source.context, source.request.id) == source.request
        assert repository.get_answer_by_id(source.context, answer_id) == streamed
        assert repository.list_statements(source.context, answer_id) == statements
        assert repository.get_request_by_id(foreign_source.context, source.request.id) is None
        assert repository.get_answer_by_id(foreign_source.context, answer_id) is None
        assert repository.list_statements(foreign_source.context, answer_id) == ()


def test_database_rejects_answer_linked_to_foreign_request(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    source = make_research_source(label="Request Answer Source")
    foreign_source = make_research_source(label="Foreign Request Answer Source")
    persist_research_source(integration_database_engine, source)
    persist_workspace(integration_database_engine, foreign_source)
    result = make_retrieval_result(source)
    cross_workspace_answer = make_answer(
        foreign_source,
        result,
        AnswerState.REQUESTED,
        answer_id=uuid4(),
        research_request_id=source.request.id,
    )

    with (
        pytest.raises(IntegrityError),
        transactional_session(session_factory) as session,
    ):
        SqlAlchemyResearchRepository(session).add_answer(
            foreign_source.context,
            cross_workspace_answer,
        )


def test_database_rejects_retrieval_run_from_another_request(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    source = make_research_source(label="Retrieval Answer Source")
    persist_research_source(integration_database_engine, source)
    result = make_retrieval_result(source)
    persist_retrieval_result(integration_database_engine, source, result)
    other_request = source.request.model_copy(
        update={"id": uuid4(), "question": "What was the operating margin?"}
    )
    answer_id = uuid4()

    with transactional_session(session_factory) as session:
        SqlAlchemyResearchRepository(session).add_request(source.context, other_request)
    with transactional_session(session_factory) as session:
        SqlAlchemyResearchRepository(session).add_answer(
            source.context,
            make_answer(
                source,
                result,
                AnswerState.REQUESTED,
                answer_id=answer_id,
                research_request_id=other_request.id,
            ),
        )
    with transactional_session(session_factory) as session:
        assert SqlAlchemyResearchRepository(session).update_answer(
            source.context,
            make_answer(
                source,
                result,
                AnswerState.RETRIEVING,
                answer_id=answer_id,
                research_request_id=other_request.id,
            ),
        )

    with (
        pytest.raises(IntegrityError),
        transactional_session(session_factory) as session,
    ):
        SqlAlchemyResearchRepository(session).update_answer(
            source.context,
            make_answer(
                source,
                result,
                AnswerState.GENERATING,
                answer_id=answer_id,
                research_request_id=other_request.id,
            ),
        )


def test_database_rejects_duplicate_answer_version_and_statement_ordinal(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    source = make_research_source(label="Versioned Answer Source")
    persist_research_source(integration_database_engine, source)
    result = make_retrieval_result(source)
    persist_retrieval_result(integration_database_engine, source, result)
    first_answer_id = uuid4()
    streamed, statements = persist_answer_through_streamed(
        integration_database_engine,
        source,
        result,
        answer_id=first_answer_id,
    )

    duplicate_version = make_answer(
        source,
        result,
        AnswerState.REQUESTED,
        answer_id=uuid4(),
    )
    with (
        pytest.raises(IntegrityError),
        transactional_session(session_factory) as session,
    ):
        SqlAlchemyResearchRepository(session).add_answer(source.context, duplicate_version)

    duplicate_ordinal = statements[1].model_copy(update={"id": uuid4(), "ordinal": 0})
    with (
        pytest.raises(IntegrityError),
        transactional_session(session_factory) as session,
    ):
        session.add(AnswerStatementRow.from_domain(duplicate_ordinal))

    assert streamed.version_number == 1
