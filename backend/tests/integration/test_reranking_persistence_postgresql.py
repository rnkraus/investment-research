"""Real database regressions for opt-in reranking records, without live providers."""

from uuid import uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.modules.search.domain import (
    RetrievalMode,
    RetrievalOrigin,
    SearchRelationshipError,
)
from investment_research.modules.search.persistence import SqlAlchemyRetrievalRepository
from investment_research.modules.search.reranking_contracts import RRF_RERANK_SELECTOR_VERSION
from tests.integration.factories import (
    ResearchSource,
    RetrievalResultFixture,
    make_research_source,
    make_retrieval_result,
    persist_research_source,
)


def make_reranking_result(source: ResearchSource, *, tied: bool = False) -> RetrievalResultFixture:
    """Keep both lane votes while letting scores override the pre-budget fusion order."""

    baseline = make_retrieval_result(source)
    run = baseline.run.model_copy(
        update={
            "mode": RetrievalMode.DUAL_LANE,
            "embedding_config_version": "openai-text-embedding-3-small-v1",
            "dense_index_config_version": "pinecone-dense-v1",
            "selector_config_version": RRF_RERANK_SELECTOR_VERSION,
            "dense_candidate_limit": 10,
            "context_chunk_limit": 8,
            "context_character_limit": 24_000,
        }
    )
    first, second = baseline.candidates
    candidates = (
        first.model_copy(
            update={
                "fused_rank": 2,
                "reranker_score": 0.2 if tied else 0.9,
                "selection_rank": 2 if tied else 1,
            }
        ),
        second.model_copy(
            update={"fused_rank": 1, "reranker_score": 0.2, "selection_rank": 1 if tied else 2}
        ),
        second.model_copy(
            update={
                "id": uuid4(),
                "origin": RetrievalOrigin.DENSE,
                "rank": 1,
                "raw_score": -0.5,
                "fused_rank": 1,
                "reranker_score": 0.2,
                "selection_rank": None,
            }
        ),
    )
    return RetrievalResultFixture(run=run, candidates=candidates)


@pytest.mark.parametrize("tied", [False, True])
def test_reranking_records_round_trip_scores_ranks_and_workspace_isolation(
    integration_database_engine: Engine, tied: bool
) -> None:
    source = make_research_source(label="Reranking persistence source")
    foreign = make_research_source(label="Foreign reranking source")
    persist_research_source(integration_database_engine, source)
    persist_research_source(integration_database_engine, foreign)
    result = make_reranking_result(source, tied=tied)
    session_factory = create_session_factory(integration_database_engine)

    with transactional_session(session_factory) as session:
        SqlAlchemyRetrievalRepository(session).add_result(
            source.context, result.run, result.candidates
        )

    with session_factory() as session:
        repository = SqlAlchemyRetrievalRepository(session)
        assert repository.get_run_by_id(source.context, result.run.id) == result.run
        stored = repository.list_candidates(source.context, result.run.id)
        assert {item.id: item for item in stored} == {item.id: item for item in result.candidates}
        assert [item.selection_rank for item in stored] == [1, 2, None]
        assert [item.fused_rank for item in stored[:2]] == ([1, 2] if tied else [2, 1])
        assert [item.reranker_score for item in stored] == (
            [0.2, 0.2, 0.2] if tied else [0.9, 0.2, 0.2]
        )
        assert repository.get_run_by_id(foreign.context, result.run.id) is None
        assert repository.list_candidates(foreign.context, result.run.id) == ()
        assert repository.get_run_by_id(source.context, uuid4()) is None
        assert repository.list_candidates(source.context, uuid4()) == ()


@pytest.mark.parametrize("kind", ["score", "fusion", "selection"])
def test_invalid_reranking_records_leave_no_partial_run(
    integration_database_engine: Engine, kind: str
) -> None:
    source = make_research_source(label=f"Invalid reranking {kind}")
    persist_research_source(integration_database_engine, source)
    result = make_reranking_result(source)
    first, second, dense = result.candidates
    if kind == "score":
        dense = dense.model_copy(update={"reranker_score": 0.3})
    elif kind == "fusion":
        first = first.model_copy(update={"fused_rank": 1})
    else:
        first = first.model_copy(update={"selection_rank": 2})
        second = second.model_copy(update={"selection_rank": 1})
    session_factory = create_session_factory(integration_database_engine)

    with pytest.raises(SearchRelationshipError), transactional_session(session_factory) as session:
        SqlAlchemyRetrievalRepository(session).add_result(
            source.context, result.run, (first, second, dense)
        )

    with session_factory() as session:
        repository = SqlAlchemyRetrievalRepository(session)
        assert repository.get_run_by_id(source.context, result.run.id) is None
        assert repository.list_candidates(source.context, result.run.id) == ()


def test_foreign_reranked_chunk_fails_at_database_boundary_and_rolls_back_run(
    integration_database_engine: Engine,
) -> None:
    source = make_research_source(label="Reranking owner")
    foreign = make_research_source(label="Reranking foreign chunk")
    persist_research_source(integration_database_engine, source)
    persist_research_source(integration_database_engine, foreign)
    result = make_reranking_result(source)
    first, second, dense = result.candidates
    session_factory = create_session_factory(integration_database_engine)

    with pytest.raises(IntegrityError), transactional_session(session_factory) as session:
        SqlAlchemyRetrievalRepository(session).add_result(
            source.context,
            result.run,
            (first.model_copy(update={"chunk_id": foreign.chunks[0].id}), second, dense),
        )

    with session_factory() as session:
        repository = SqlAlchemyRetrievalRepository(session)
        assert repository.get_run_by_id(source.context, result.run.id) is None
        assert repository.list_candidates(source.context, result.run.id) == ()


def test_caller_owned_transaction_rolls_back_valid_reranking_records(
    integration_database_engine: Engine,
) -> None:
    source = make_research_source(label="Reranking caller rollback")
    persist_research_source(integration_database_engine, source)
    result = make_reranking_result(source)
    session_factory = create_session_factory(integration_database_engine)

    with (
        pytest.raises(RuntimeError, match="synthetic caller failure"),
        transactional_session(session_factory) as session,
    ):
        SqlAlchemyRetrievalRepository(session).add_result(
            source.context, result.run, result.candidates
        )
        session.flush()
        raise RuntimeError("synthetic caller failure")

    with session_factory() as session:
        assert (
            SqlAlchemyRetrievalRepository(session).get_run_by_id(source.context, result.run.id)
            is None
        )


def test_empty_reranking_result_is_persisted_without_invented_scores(
    integration_database_engine: Engine,
) -> None:
    source = make_research_source(label="Empty reranking persistence")
    persist_research_source(integration_database_engine, source)
    result = make_reranking_result(source)
    session_factory = create_session_factory(integration_database_engine)

    with transactional_session(session_factory) as session:
        SqlAlchemyRetrievalRepository(session).add_result(source.context, result.run, ())
    with session_factory() as session:
        repository = SqlAlchemyRetrievalRepository(session)
        assert repository.get_run_by_id(source.context, result.run.id) == result.run
        assert repository.list_candidates(source.context, result.run.id) == ()
