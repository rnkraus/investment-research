"""PostgreSQL integration tests for scoped retrieval persistence."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.modules.search.domain import (
    LEXICAL_ORDER_SELECTOR_CONFIG_VERSION,
    RetrievalCandidate,
    RetrievalFilters,
    RetrievalMode,
    RetrievalOrigin,
    RetrievalRun,
)
from investment_research.modules.search.persistence import (
    RetrievalCandidateRow,
    SqlAlchemyRetrievalRepository,
)
from tests.integration.factories import (
    FIXTURE_TIME,
    ResearchSource,
    make_research_source,
    persist_research_source,
    persist_workspace,
)


def make_retrieval_run(
    source: ResearchSource,
    *,
    workspace_id: UUID | None = None,
    research_request_id: UUID | None = None,
    mode: RetrievalMode = RetrievalMode.LEXICAL,
) -> RetrievalRun:
    """Build one completed retrieval run for a synthetic research source."""

    is_dual_lane = mode is RetrievalMode.DUAL_LANE
    return RetrievalRun(
        id=uuid4(),
        workspace_id=workspace_id or source.context.workspace_id,
        research_request_id=research_request_id or source.request.id,
        query_text="  What was FY2025 revenue?  ",
        normalized_query="fy2025 revenue",
        filters=RetrievalFilters(document_ids=(source.document.id,)),
        mode=mode,
        lexical_config_version=source.processing_run.lexical_config_version,
        embedding_config_version="openai-text-embedding-3-small-v1" if is_dual_lane else None,
        dense_index_config_version="pinecone-dense-v1" if is_dual_lane else None,
        selector_config_version=(
            "deterministic-round-robin-v1"
            if is_dual_lane
            else LEXICAL_ORDER_SELECTOR_CONFIG_VERSION
        ),
        lexical_candidate_limit=10,
        dense_candidate_limit=10 if is_dual_lane else 0,
        context_chunk_limit=8 if is_dual_lane else None,
        context_character_limit=24_000 if is_dual_lane else None,
        started_at=FIXTURE_TIME + timedelta(seconds=4),
        completed_at=FIXTURE_TIME + timedelta(seconds=5),
    )


def make_candidate(
    source: ResearchSource,
    run: RetrievalRun,
    *,
    chunk_index: int,
    rank: int,
    chunk_id: UUID | None = None,
    origin: RetrievalOrigin = RetrievalOrigin.LEXICAL,
    raw_score: float | None = None,
    selection_rank: int | None = None,
) -> RetrievalCandidate:
    """Build one ranked retrieval candidate connected to a source chunk."""

    return RetrievalCandidate(
        id=uuid4(),
        workspace_id=run.workspace_id,
        retrieval_run_id=run.id,
        chunk_id=chunk_id or source.chunks[chunk_index].id,
        origin=origin,
        rank=rank,
        raw_score=(0.75 / rank) if raw_score is None else raw_score,
        selection_rank=selection_rank,
        created_at=run.completed_at,
    )


def test_retrieval_result_round_trips_in_stable_workspace_scoped_order(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    source = make_research_source(label="Search Source")
    foreign_source = make_research_source(label="Foreign Search Source")
    persist_research_source(integration_database_engine, source)
    persist_research_source(integration_database_engine, foreign_source)
    run = make_retrieval_run(source, mode=RetrievalMode.DUAL_LANE)
    first_lexical = make_candidate(
        source,
        run,
        chunk_index=0,
        rank=1,
        selection_rank=2,
    )
    first_dense = make_candidate(
        source,
        run,
        chunk_index=1,
        rank=1,
        origin=RetrievalOrigin.DENSE,
        raw_score=-0.125,
        selection_rank=1,
    )

    with transactional_session(session_factory) as session:
        SqlAlchemyRetrievalRepository(session).add_result(
            source.context,
            run,
            [first_lexical, first_dense],
        )

    with transactional_session(session_factory) as session:
        repository = SqlAlchemyRetrievalRepository(session)
        assert repository.get_run_by_id(source.context, run.id) == run
        assert repository.list_candidates(source.context, run.id) == (
            first_dense,
            first_lexical,
        )
        assert repository.get_run_by_id(foreign_source.context, run.id) is None
        assert repository.list_candidates(foreign_source.context, run.id) == ()


def test_database_rejects_retrieval_run_linked_to_foreign_request(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    source = make_research_source(label="Request Source")
    foreign_source = make_research_source(label="Foreign Request Source")
    persist_research_source(integration_database_engine, source)
    persist_workspace(integration_database_engine, foreign_source)
    cross_workspace_run = make_retrieval_run(
        source,
        workspace_id=foreign_source.context.workspace_id,
        research_request_id=source.request.id,
    )

    with (
        pytest.raises(IntegrityError),
        transactional_session(session_factory) as session,
    ):
        SqlAlchemyRetrievalRepository(session).add_result(
            foreign_source.context,
            cross_workspace_run,
            [],
        )


def test_database_rejects_candidate_linked_to_foreign_chunk(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    source = make_research_source(label="Candidate Source")
    foreign_source = make_research_source(label="Foreign Candidate Source")
    persist_research_source(integration_database_engine, source)
    persist_research_source(integration_database_engine, foreign_source)
    run = make_retrieval_run(source)

    with transactional_session(session_factory) as session:
        SqlAlchemyRetrievalRepository(session).add_result(source.context, run, [])

    cross_workspace_candidate = make_candidate(
        source,
        run,
        chunk_index=0,
        rank=1,
        chunk_id=foreign_source.chunks[0].id,
    )
    with (
        pytest.raises(IntegrityError),
        transactional_session(session_factory) as session,
    ):
        session.add(RetrievalCandidateRow.from_domain(cross_workspace_candidate))


def test_database_rejects_duplicate_origin_rank(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    source = make_research_source(label="Rank Source")
    persist_research_source(integration_database_engine, source)
    run = make_retrieval_run(source)
    first_candidate = make_candidate(source, run, chunk_index=0, rank=1)

    with transactional_session(session_factory) as session:
        SqlAlchemyRetrievalRepository(session).add_result(
            source.context,
            run,
            [first_candidate],
        )

    duplicate_rank = make_candidate(source, run, chunk_index=1, rank=1)
    with (
        pytest.raises(IntegrityError),
        transactional_session(session_factory) as session,
    ):
        session.add(RetrievalCandidateRow.from_domain(duplicate_rank))
