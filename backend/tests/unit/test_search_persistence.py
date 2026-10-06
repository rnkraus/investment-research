"""Unit tests for retrieval domain values, mappings, and scoped persistence."""

from datetime import UTC, datetime, timedelta
from math import inf, nan
from typing import cast
from unittest.mock import create_autospec
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import ForeignKeyConstraint, Table
from sqlalchemy.orm import Session

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.contracts import M1_SELECTOR_CONFIG_VERSION
from investment_research.modules.search.domain import (
    LEXICAL_ORDER_SELECTOR_CONFIG_VERSION,
    RetrievalCandidate,
    RetrievalFilters,
    RetrievalMode,
    RetrievalOrigin,
    RetrievalRun,
    SearchRelationshipError,
    SearchScopeError,
)
from investment_research.modules.search.fusion_contracts import RRF_CONFIG_VERSION
from investment_research.modules.search.persistence import (
    RetrievalCandidateRow,
    RetrievalRunRow,
    SqlAlchemyRetrievalRepository,
)
from investment_research.modules.search.ports import RetrievalRepository

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
OTHER_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000003")
DOCUMENT_ID = UUID("30000000-0000-4000-8000-000000000001")
RESEARCH_REQUEST_ID = UUID("30000000-0000-4000-8000-000000000002")
RETRIEVAL_RUN_ID = UUID("30000000-0000-4000-8000-000000000003")
CANDIDATE_ID = UUID("30000000-0000-4000-8000-000000000004")
OTHER_CANDIDATE_ID = UUID("30000000-0000-4000-8000-000000000005")
CHUNK_ID = UUID("30000000-0000-4000-8000-000000000006")
OTHER_CHUNK_ID = UUID("30000000-0000-4000-8000-000000000007")
OTHER_ID = UUID("30000000-0000-4000-8000-000000000008")
STARTED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)
COMPLETED_AT = STARTED_AT + timedelta(milliseconds=25)


def make_context() -> WorkspaceContext:
    """Build the deterministic trusted context used by search tests."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)


def make_run(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    lexical_candidate_limit: int = 10,
) -> RetrievalRun:
    """Build one reproducible completed lexical retrieval run."""

    return RetrievalRun(
        id=RETRIEVAL_RUN_ID,
        workspace_id=workspace_id,
        research_request_id=RESEARCH_REQUEST_ID,
        query_text="  What was FY2025 revenue?  ",
        normalized_query="fy2025 revenue",
        filters=RetrievalFilters(
            document_ids=(DOCUMENT_ID,),
            available_at_cutoff=STARTED_AT - timedelta(days=1),
        ),
        mode=RetrievalMode.LEXICAL,
        lexical_config_version="english-lexical-v1",
        selector_config_version=LEXICAL_ORDER_SELECTOR_CONFIG_VERSION,
        lexical_candidate_limit=lexical_candidate_limit,
        started_at=STARTED_AT,
        completed_at=COMPLETED_AT,
    )


def make_dual_lane_run(*, context_chunk_limit: int = 8) -> RetrievalRun:
    """Build one reproducible M1 lexical-plus-dense retrieval run."""

    return RetrievalRun(
        id=RETRIEVAL_RUN_ID,
        workspace_id=WORKSPACE_ID,
        research_request_id=RESEARCH_REQUEST_ID,
        query_text="  What was FY2025 revenue?  ",
        normalized_query="fy2025 revenue",
        filters=RetrievalFilters(document_ids=(DOCUMENT_ID,)),
        mode=RetrievalMode.DUAL_LANE,
        lexical_config_version="english-lexical-v1",
        embedding_config_version="openai-text-embedding-3-small-v1",
        dense_index_config_version="pinecone-dense-v1",
        selector_config_version=M1_SELECTOR_CONFIG_VERSION,
        lexical_candidate_limit=10,
        dense_candidate_limit=10,
        context_chunk_limit=context_chunk_limit,
        context_character_limit=24_000,
        started_at=STARTED_AT,
        completed_at=COMPLETED_AT,
    )


def make_candidate(
    *,
    candidate_id: UUID = CANDIDATE_ID,
    workspace_id: UUID = WORKSPACE_ID,
    retrieval_run_id: UUID = RETRIEVAL_RUN_ID,
    chunk_id: UUID = CHUNK_ID,
    rank: int = 1,
    origin: RetrievalOrigin = RetrievalOrigin.LEXICAL,
    raw_score: float = 0.75,
    selection_rank: int | None = None,
    fused_rank: int | None = None,
) -> RetrievalCandidate:
    """Build one deterministic ranked retrieval candidate."""

    return RetrievalCandidate(
        id=candidate_id,
        workspace_id=workspace_id,
        retrieval_run_id=retrieval_run_id,
        chunk_id=chunk_id,
        origin=origin,
        rank=rank,
        raw_score=raw_score,
        selection_rank=selection_rank,
        fused_rank=fused_rank,
        created_at=COMPLETED_AT,
    )


def make_rrf_candidates() -> tuple[RetrievalCandidate, ...]:
    """Give the second lexical chunk a shared dense vote and first fusion rank."""

    return (
        make_candidate(fused_rank=2, selection_rank=2),
        make_candidate(
            candidate_id=OTHER_CANDIDATE_ID,
            chunk_id=OTHER_CHUNK_ID,
            rank=2,
            fused_rank=1,
            selection_rank=1,
        ),
        make_candidate(
            candidate_id=OTHER_ID,
            chunk_id=OTHER_CHUNK_ID,
            origin=RetrievalOrigin.DENSE,
            raw_score=-0.9,
            fused_rank=1,
        ),
    )


def test_repository_accepts_reconstructable_rrf_ranks_and_round_trips_fields() -> None:
    session = create_autospec(Session, instance=True)
    run = make_dual_lane_run().model_copy(update={"selector_config_version": RRF_CONFIG_VERSION})
    candidates = make_rrf_candidates()

    SqlAlchemyRetrievalRepository(session).add_result(make_context(), run, candidates)

    session.add.assert_called_once()
    assert (
        tuple(RetrievalCandidateRow.from_domain(item).to_domain() for item in candidates)
        == candidates
    )


@pytest.mark.parametrize("fused_rank", [None, 1, 3])
def test_repository_rejects_invented_rrf_rank_before_staging(fused_rank: int | None) -> None:
    session = create_autospec(Session, instance=True)
    run = make_dual_lane_run().model_copy(update={"selector_config_version": RRF_CONFIG_VERSION})
    candidates = make_rrf_candidates()
    changed = (candidates[0].model_copy(update={"fused_rank": fused_rank}), *candidates[1:])

    with pytest.raises(SearchRelationshipError, match="fusion ranks do not match"):
        SqlAlchemyRetrievalRepository(session).add_result(make_context(), run, changed)
    session.add.assert_not_called()
    session.flush.assert_not_called()
    session.add_all.assert_not_called()


def test_repository_rejects_rrf_selection_that_reverses_fusion_order() -> None:
    session = create_autospec(Session, instance=True)
    run = make_dual_lane_run().model_copy(update={"selector_config_version": RRF_CONFIG_VERSION})
    first, second, dense = make_rrf_candidates()
    changed = (
        first.model_copy(update={"selection_rank": 1}),
        second.model_copy(update={"selection_rank": 2}),
        dense,
    )
    with pytest.raises(SearchRelationshipError, match="preserve fusion order"):
        SqlAlchemyRetrievalRepository(session).add_result(make_context(), run, changed)
    session.add.assert_not_called()


def test_repository_accepts_budget_gaps_and_empty_rrf_results() -> None:
    run = make_dual_lane_run().model_copy(update={"selector_config_version": RRF_CONFIG_VERSION})
    first, second, dense = make_rrf_candidates()
    for candidates in (
        (),
        (
            first.model_copy(update={"selection_rank": 1}),
            second.model_copy(update={"selection_rank": None}),
            dense,
        ),
    ):
        session = create_autospec(Session, instance=True)
        SqlAlchemyRetrievalRepository(session).add_result(make_context(), run, candidates)
        session.add.assert_called_once()


@pytest.mark.parametrize(
    "changes",
    [
        {"lexical_candidate_limit": 21},
        {"dense_candidate_limit": 21},
    ],
)
def test_repository_rejects_rrf_lane_limits_beyond_the_versioned_policy(
    changes: dict[str, object],
) -> None:
    session = create_autospec(Session, instance=True)
    run = make_dual_lane_run().model_copy(
        update={"selector_config_version": RRF_CONFIG_VERSION, **changes}
    )
    with pytest.raises(SearchRelationshipError, match="bounded dual-lane"):
        SqlAlchemyRetrievalRepository(session).add_result(make_context(), run, ())
    session.add.assert_not_called()


def test_repository_rejects_rrf_policy_on_lexical_only_run() -> None:
    session = create_autospec(Session, instance=True)
    run = make_run().model_copy(update={"selector_config_version": RRF_CONFIG_VERSION})
    with pytest.raises(SearchRelationshipError, match="bounded dual-lane"):
        SqlAlchemyRetrievalRepository(session).add_result(make_context(), run, ())
    session.add.assert_not_called()


def test_retrieval_run_preserves_query_and_round_trips_filter_snapshot() -> None:
    run = make_run()

    row = RetrievalRunRow.from_domain(run)

    assert run.query_text == "  What was FY2025 revenue?  "
    assert row.to_domain() == run
    assert row.filter_snapshot == {
        "document_ids": [str(DOCUMENT_ID)],
        "available_at_cutoff": "2026-02-19T12:00:00Z",
        "captured_at_cutoff": None,
    }


def test_retrieval_filters_reject_duplicate_document_ids() -> None:
    with pytest.raises(ValidationError, match="document_ids must be unique"):
        RetrievalFilters(document_ids=(DOCUMENT_ID, DOCUMENT_ID))


@pytest.mark.parametrize(
    ("changes", "expected_error"),
    [
        pytest.param(
            {"query_text": " \n\t "},
            "non-whitespace",
            id="empty-query",
        ),
        pytest.param(
            {"completed_at": STARTED_AT - timedelta(microseconds=1)},
            "must not precede",
            id="reversed-times",
        ),
        pytest.param(
            {"dense_candidate_limit": 1},
            "LEXICAL retrieval must not define",
            id="lexical-with-dense-settings",
        ),
        pytest.param(
            {"mode": RetrievalMode.DUAL_LANE},
            "DUAL_LANE retrieval requires",
            id="dual-lane-without-dense-settings",
        ),
    ],
)
def test_retrieval_run_rejects_invalid_snapshots(
    changes: dict[str, object],
    expected_error: str,
) -> None:
    payload = {**make_run().model_dump(), **changes}

    with pytest.raises(ValidationError, match=expected_error):
        RetrievalRun.model_validate(payload)


@pytest.mark.parametrize(
    "raw_score",
    [inf, -inf, nan],
    ids=["positive-infinity", "negative-infinity", "not-a-number"],
)
def test_retrieval_candidate_rejects_invalid_raw_scores(raw_score: float) -> None:
    payload = {**make_candidate().model_dump(), "raw_score": raw_score}

    with pytest.raises(ValidationError):
        RetrievalCandidate.model_validate(payload)


def test_retrieval_candidate_accepts_negative_finite_dense_score() -> None:
    candidate = make_candidate(
        origin=RetrievalOrigin.DENSE,
        raw_score=-0.125,
    )

    assert candidate.raw_score == -0.125


def test_repository_matches_application_owned_protocol() -> None:
    session = create_autospec(Session, instance=True)

    repository: RetrievalRepository = SqlAlchemyRetrievalRepository(session)

    assert isinstance(repository, SqlAlchemyRetrievalRepository)


def test_repository_stages_completed_run_and_candidates() -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyRetrievalRepository(session)
    run = make_run()
    candidate = make_candidate()

    repository.add_result(make_context(), run, [candidate])

    session.add.assert_called_once()
    run_row = session.add.call_args.args[0]
    assert isinstance(run_row, RetrievalRunRow)
    assert run_row.to_domain() == run
    session.flush.assert_called_once_with()

    session.add_all.assert_called_once()
    candidate_rows = list(session.add_all.call_args.args[0])
    assert len(candidate_rows) == 1
    assert isinstance(candidate_rows[0], RetrievalCandidateRow)
    assert candidate_rows[0].to_domain() == candidate


def test_repository_stages_ranked_dual_lane_result() -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyRetrievalRepository(session)
    run = make_dual_lane_run()
    candidates = [
        make_candidate(selection_rank=1),
        make_candidate(
            candidate_id=OTHER_CANDIDATE_ID,
            chunk_id=OTHER_CHUNK_ID,
            origin=RetrievalOrigin.DENSE,
            raw_score=-0.125,
            selection_rank=2,
        ),
    ]

    repository.add_result(make_context(), run, candidates)

    stored_candidates = [row.to_domain() for row in session.add_all.call_args.args[0]]
    assert stored_candidates == candidates


def test_repository_allows_empty_insufficient_result() -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyRetrievalRepository(session)

    repository.add_result(make_context(), make_run(), [])

    session.add.assert_called_once()
    assert list(session.add_all.call_args.args[0]) == []


@pytest.mark.parametrize(
    ("run", "candidates", "error_type", "expected_error"),
    [
        pytest.param(
            make_run(workspace_id=OTHER_WORKSPACE_ID),
            [],
            SearchScopeError,
            "outside the active workspace",
            id="run-outside-workspace",
        ),
        pytest.param(
            make_run(),
            [make_candidate(workspace_id=OTHER_WORKSPACE_ID)],
            SearchScopeError,
            "outside the active workspace",
            id="candidate-outside-workspace",
        ),
        pytest.param(
            make_run(),
            [make_candidate(retrieval_run_id=OTHER_ID)],
            SearchRelationshipError,
            "does not belong",
            id="wrong-run",
        ),
        pytest.param(
            make_run(),
            [make_candidate(rank=2)],
            SearchRelationshipError,
            "contiguous",
            id="rank-gap",
        ),
        pytest.param(
            make_run(),
            [
                make_candidate(),
                make_candidate(candidate_id=OTHER_CANDIDATE_ID, rank=2),
            ],
            SearchRelationshipError,
            "duplicated",
            id="duplicate-chunk",
        ),
        pytest.param(
            make_run(lexical_candidate_limit=1),
            [
                make_candidate(),
                make_candidate(
                    candidate_id=OTHER_CANDIDATE_ID,
                    chunk_id=OTHER_CHUNK_ID,
                    rank=2,
                ),
            ],
            SearchRelationshipError,
            "exceeds",
            id="over-limit",
        ),
        pytest.param(
            make_run(),
            [make_candidate(origin=RetrievalOrigin.DENSE)],
            SearchRelationshipError,
            "origin is not enabled",
            id="dense-candidate-in-lexical-run",
        ),
        pytest.param(
            make_dual_lane_run(),
            [make_candidate(selection_rank=2)],
            SearchRelationshipError,
            "selection ranks must be contiguous",
            id="selection-rank-gap",
        ),
        pytest.param(
            make_dual_lane_run(),
            [
                make_candidate(selection_rank=1),
                make_candidate(
                    candidate_id=OTHER_CANDIDATE_ID,
                    origin=RetrievalOrigin.DENSE,
                    selection_rank=2,
                ),
            ],
            SearchRelationshipError,
            "selected candidate chunks must be unique",
            id="duplicate-selected-chunk",
        ),
        pytest.param(
            make_dual_lane_run(context_chunk_limit=1),
            [
                make_candidate(selection_rank=1),
                make_candidate(
                    candidate_id=OTHER_CANDIDATE_ID,
                    chunk_id=OTHER_CHUNK_ID,
                    origin=RetrievalOrigin.DENSE,
                    selection_rank=2,
                ),
            ],
            SearchRelationshipError,
            "selected candidate count exceeds",
            id="over-context-limit",
        ),
    ],
)
def test_repository_rejects_inconsistent_results(
    run: RetrievalRun,
    candidates: list[RetrievalCandidate],
    error_type: type[ValueError],
    expected_error: str,
) -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyRetrievalRepository(session)

    with pytest.raises(error_type, match=expected_error):
        repository.add_result(make_context(), run, candidates)

    session.add.assert_not_called()
    session.add_all.assert_not_called()


def test_repository_reads_run_without_cross_workspace_disclosure() -> None:
    session = create_autospec(Session, instance=True)
    stored_row = RetrievalRunRow.from_domain(make_run())
    repository = SqlAlchemyRetrievalRepository(session)

    session.scalar.return_value = stored_row
    assert repository.get_run_by_id(make_context(), RETRIEVAL_RUN_ID) == make_run()

    session.scalar.return_value = None
    assert repository.get_run_by_id(make_context(), OTHER_ID) is None


def test_repository_lists_candidates_only_for_known_scoped_run() -> None:
    session = create_autospec(Session, instance=True)
    candidate = make_candidate()
    session.scalar.return_value = RETRIEVAL_RUN_ID
    session.scalars.return_value = (RetrievalCandidateRow.from_domain(candidate),)
    repository = SqlAlchemyRetrievalRepository(session)

    assert repository.list_candidates(make_context(), RETRIEVAL_RUN_ID) == (candidate,)

    session.scalar.return_value = None
    session.scalars.reset_mock()
    assert repository.list_candidates(make_context(), OTHER_ID) == ()
    session.scalars.assert_not_called()


def test_retrieval_tables_enforce_workspace_relationships() -> None:
    run_table = cast(Table, RetrievalRunRow.__table__)
    candidate_table = cast(Table, RetrievalCandidateRow.__table__)

    for table in (run_table, candidate_table):
        assert "workspace_id" in table.c
        target_sets = {
            tuple(element.target_fullname for element in constraint.elements)
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
        }
        assert ("workspaces.id",) in target_sets

    run_targets = {
        tuple(element.target_fullname for element in constraint.elements)
        for constraint in run_table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert ("research_requests.workspace_id", "research_requests.id") in run_targets

    candidate_targets = {
        tuple(element.target_fullname for element in constraint.elements)
        for constraint in candidate_table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert ("retrieval_runs.workspace_id", "retrieval_runs.id") in candidate_targets
    assert ("chunks.workspace_id", "chunks.id") in candidate_targets
