"""Unit tests for research requests, answer lifecycles, and scoped persistence."""

from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import create_autospec
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import ForeignKeyConstraint, Table
from sqlalchemy.orm import Session

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.research.domain import (
    Answer,
    AnswerState,
    AnswerStatement,
    CoverageStatus,
    InvalidAnswerTransitionError,
    ResearchRelationshipError,
    ResearchRequest,
    ResearchScopeError,
    StatementSupportStatus,
    StatementType,
    TemporalMode,
)
from investment_research.modules.research.persistence import (
    AnswerRow,
    AnswerStatementRow,
    ResearchRequestRow,
    SqlAlchemyResearchRepository,
)
from investment_research.modules.research.ports import ResearchRepository

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
OTHER_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000003")
DOCUMENT_ID = UUID("40000000-0000-4000-8000-000000000001")
RESEARCH_REQUEST_ID = UUID("40000000-0000-4000-8000-000000000002")
RETRIEVAL_RUN_ID = UUID("40000000-0000-4000-8000-000000000003")
ANSWER_ID = UUID("40000000-0000-4000-8000-000000000004")
STATEMENT_ID = UUID("40000000-0000-4000-8000-000000000005")
OTHER_STATEMENT_ID = UUID("40000000-0000-4000-8000-000000000006")
OTHER_ID = UUID("40000000-0000-4000-8000-000000000007")
CREATED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)
COMPLETED_AT = CREATED_AT + timedelta(seconds=1)


def make_context() -> WorkspaceContext:
    """Build the deterministic trusted context used by research tests."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)


def make_request(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    temporal_mode: TemporalMode = TemporalMode.CURRENT,
) -> ResearchRequest:
    """Build one immutable research request with consistent temporal context."""

    cutoff_at = None if temporal_mode is TemporalMode.CURRENT else CREATED_AT - timedelta(days=1)
    return ResearchRequest(
        id=RESEARCH_REQUEST_ID,
        workspace_id=workspace_id,
        question="  What was FY2025 revenue?  ",
        document_ids=(DOCUMENT_ID,),
        temporal_mode=temporal_mode,
        cutoff_at=cutoff_at,
        created_at=CREATED_AT,
    )


def make_answer(
    state: AnswerState = AnswerState.REQUESTED,
    *,
    workspace_id: UUID = WORKSPACE_ID,
) -> Answer:
    """Build a lifecycle-consistent answer snapshot."""

    retrieval_run_id = (
        None if state in {AnswerState.REQUESTED, AnswerState.RETRIEVING} else RETRIEVAL_RUN_ID
    )
    terminal_states = {
        AnswerState.STREAMED,
        AnswerState.INSUFFICIENT,
        AnswerState.REJECTED,
        AnswerState.REGENERATED,
    }
    completed_at = COMPLETED_AT if state in terminal_states else None
    coverage_status = None
    rendered_text = None
    if state is AnswerState.STREAMED:
        coverage_status = CoverageStatus.COMPLETE
        rendered_text = "FY2025 revenue was $184.2 million."
    elif state is AnswerState.INSUFFICIENT:
        coverage_status = CoverageStatus.INSUFFICIENT
        rendered_text = "The archive does not contain sufficient evidence."

    return Answer(
        id=ANSWER_ID,
        workspace_id=workspace_id,
        research_request_id=RESEARCH_REQUEST_ID,
        retrieval_run_id=retrieval_run_id,
        version_number=1,
        state=state,
        coverage_status=coverage_status,
        generator_version="fixture-generator-1",
        generation_config_version="generation-m0.0-v1",
        prompt_version="answer-prompt-v1",
        rendered_text=rendered_text,
        created_at=CREATED_AT,
        completed_at=completed_at,
    )


def make_statement(
    *,
    statement_id: UUID = STATEMENT_ID,
    workspace_id: UUID = WORKSPACE_ID,
    answer_id: UUID = ANSWER_ID,
    ordinal: int = 0,
) -> AnswerStatement:
    """Build one deterministic validated answer statement."""

    return AnswerStatement(
        id=statement_id,
        workspace_id=workspace_id,
        answer_id=answer_id,
        ordinal=ordinal,
        text="FY2025 revenue was $184.2 million.",
        statement_type=StatementType.ARCHIVE_DERIVED,
        support_status=StatementSupportStatus.VALIDATED,
        created_at=COMPLETED_AT,
    )


def test_research_request_preserves_question_and_round_trips_document_filters() -> None:
    request = make_request()

    row = ResearchRequestRow.from_domain(request)

    assert request.question == "  What was FY2025 revenue?  "
    assert row.document_ids == [DOCUMENT_ID]
    assert row.to_domain() == request


@pytest.mark.parametrize(
    ("temporal_mode", "cutoff_at", "expected_error"),
    [
        pytest.param(
            TemporalMode.CURRENT,
            CREATED_AT,
            "must not define",
            id="current-with-cutoff",
        ),
        pytest.param(
            TemporalMode.PUBLIC_KNOWLEDGE,
            None,
            "requires cutoff",
            id="historical-without-cutoff",
        ),
    ],
)
def test_research_request_rejects_inconsistent_temporal_context(
    temporal_mode: TemporalMode,
    cutoff_at: datetime | None,
    expected_error: str,
) -> None:
    payload = {
        **make_request().model_dump(),
        "temporal_mode": temporal_mode,
        "cutoff_at": cutoff_at,
    }

    with pytest.raises(ValidationError, match=expected_error):
        ResearchRequest.model_validate(payload)


def test_research_request_rejects_duplicate_document_filters() -> None:
    payload = {
        **make_request().model_dump(),
        "document_ids": (DOCUMENT_ID, DOCUMENT_ID),
    }

    with pytest.raises(ValidationError, match="document_ids must be unique"):
        ResearchRequest.model_validate(payload)


@pytest.mark.parametrize(
    ("state", "changes", "expected_error"),
    [
        pytest.param(
            AnswerState.REQUESTED,
            {"retrieval_run_id": RETRIEVAL_RUN_ID},
            "must not define retrieval_run_id",
            id="early-retrieval-reference",
        ),
        pytest.param(
            AnswerState.GENERATING,
            {"retrieval_run_id": None},
            "requires retrieval_run_id",
            id="generation-without-retrieval",
        ),
        pytest.param(
            AnswerState.STREAMED,
            {"rendered_text": None},
            "requires rendered_text",
            id="streamed-without-text",
        ),
        pytest.param(
            AnswerState.INSUFFICIENT,
            {"coverage_status": CoverageStatus.COMPLETE},
            "requires insufficient coverage",
            id="insufficient-with-complete-coverage",
        ),
        pytest.param(
            AnswerState.VALIDATING,
            {"rendered_text": "unvalidated output"},
            "only user-visible terminal answers",
            id="unvalidated-rendered-output",
        ),
        pytest.param(
            AnswerState.REJECTED,
            {"completed_at": None},
            "requires completed_at",
            id="terminal-without-completion",
        ),
        pytest.param(
            AnswerState.STREAMED,
            {"completed_at": CREATED_AT - timedelta(microseconds=1)},
            "must not precede",
            id="reversed-completion",
        ),
    ],
)
def test_answer_rejects_inconsistent_lifecycle_snapshots(
    state: AnswerState,
    changes: dict[str, object],
    expected_error: str,
) -> None:
    payload = {**make_answer(state).model_dump(), **changes}

    with pytest.raises(ValidationError, match=expected_error):
        Answer.model_validate(payload)


def test_answer_state_allows_only_declared_next_transitions() -> None:
    assert AnswerState.REQUESTED.can_transition_to(AnswerState.RETRIEVING)
    assert AnswerState.VALIDATING.can_transition_to(AnswerState.STREAMED)
    assert AnswerState.REJECTED.can_transition_to(AnswerState.REGENERATED)
    assert not AnswerState.REQUESTED.can_transition_to(AnswerState.GENERATING)
    assert not AnswerState.STREAMED.can_transition_to(AnswerState.RETRIEVING)


def test_repository_matches_application_owned_protocol() -> None:
    session = create_autospec(Session, instance=True)

    repository: ResearchRepository = SqlAlchemyResearchRepository(session)

    assert isinstance(repository, SqlAlchemyResearchRepository)


def test_repository_stages_request_only_in_matching_scope() -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyResearchRepository(session)
    request = make_request()

    repository.add_request(make_context(), request)

    session.add.assert_called_once()
    row = session.add.call_args.args[0]
    assert isinstance(row, ResearchRequestRow)
    assert row.to_domain() == request

    with pytest.raises(ResearchScopeError, match="outside the active workspace"):
        repository.add_request(
            make_context(),
            make_request(workspace_id=OTHER_WORKSPACE_ID),
        )


def test_repository_stages_only_newly_requested_answer() -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyResearchRepository(session)
    answer = make_answer()

    repository.add_answer(make_context(), answer)

    row = session.add.call_args.args[0]
    assert isinstance(row, AnswerRow)
    assert row.to_domain() == answer

    with pytest.raises(ResearchRelationshipError, match="must start in REQUESTED"):
        repository.add_answer(make_context(), make_answer(AnswerState.GENERATING))


def test_repository_advances_answer_by_one_transition() -> None:
    session = create_autospec(Session, instance=True)
    stored_row = AnswerRow.from_domain(make_answer())
    session.scalar.return_value = stored_row
    repository = SqlAlchemyResearchRepository(session)
    retrieving = make_answer(AnswerState.RETRIEVING)

    updated = repository.update_answer(make_context(), retrieving)

    assert updated is True
    assert stored_row.to_domain() == retrieving


def test_repository_sets_retrieval_once_when_generation_begins() -> None:
    session = create_autospec(Session, instance=True)
    stored_row = AnswerRow.from_domain(make_answer(AnswerState.RETRIEVING))
    session.scalar.return_value = stored_row
    repository = SqlAlchemyResearchRepository(session)

    updated = repository.update_answer(make_context(), make_answer(AnswerState.GENERATING))

    assert updated is True
    assert stored_row.retrieval_run_id == RETRIEVAL_RUN_ID


def test_repository_rejects_skipped_transition_and_changed_configuration() -> None:
    session = create_autospec(Session, instance=True)
    stored_row = AnswerRow.from_domain(make_answer())
    session.scalar.return_value = stored_row
    repository = SqlAlchemyResearchRepository(session)

    with pytest.raises(InvalidAnswerTransitionError, match="cannot transition"):
        repository.update_answer(make_context(), make_answer(AnswerState.GENERATING))

    changed_config = make_answer(AnswerState.RETRIEVING).model_copy(
        update={"prompt_version": "other-prompt"}
    )
    with pytest.raises(ResearchRelationshipError, match="configuration changed"):
        repository.update_answer(make_context(), changed_config)


def test_repository_reports_unknown_request_and_answer_without_disclosure() -> None:
    session = create_autospec(Session, instance=True)
    session.scalar.return_value = None
    repository = SqlAlchemyResearchRepository(session)

    assert repository.get_request_by_id(make_context(), OTHER_ID) is None
    assert repository.get_answer_by_id(make_context(), OTHER_ID) is None
    assert repository.update_answer(make_context(), make_answer(AnswerState.RETRIEVING)) is False


def test_repository_stages_validated_statements_in_display_order() -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyResearchRepository(session)
    answer = make_answer(AnswerState.STREAMED)
    statements = [
        make_statement(),
        make_statement(
            statement_id=OTHER_STATEMENT_ID,
            ordinal=1,
        ),
    ]

    repository.add_statements(make_context(), answer, statements)

    rows = list(session.add_all.call_args.args[0])
    assert [row.to_domain() for row in rows] == statements


@pytest.mark.parametrize(
    ("answer", "statements", "error_type", "expected_error"),
    [
        pytest.param(
            make_answer(AnswerState.INSUFFICIENT),
            [make_statement()],
            ResearchRelationshipError,
            "require a STREAMED answer",
            id="insufficient-answer",
        ),
        pytest.param(
            make_answer(AnswerState.STREAMED),
            [],
            ResearchRelationshipError,
            "requires statements",
            id="empty-statements",
        ),
        pytest.param(
            make_answer(AnswerState.STREAMED),
            [make_statement(workspace_id=OTHER_WORKSPACE_ID)],
            ResearchScopeError,
            "outside the active workspace",
            id="statement-outside-workspace",
        ),
        pytest.param(
            make_answer(AnswerState.STREAMED),
            [make_statement(answer_id=OTHER_ID)],
            ResearchRelationshipError,
            "does not belong",
            id="wrong-answer",
        ),
        pytest.param(
            make_answer(AnswerState.STREAMED),
            [make_statement(ordinal=1)],
            ResearchRelationshipError,
            "contiguous",
            id="ordinal-gap",
        ),
    ],
)
def test_repository_rejects_inconsistent_statements(
    answer: Answer,
    statements: list[AnswerStatement],
    error_type: type[ValueError],
    expected_error: str,
) -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyResearchRepository(session)

    with pytest.raises(error_type, match=expected_error):
        repository.add_statements(make_context(), answer, statements)

    session.add_all.assert_not_called()


def test_repository_lists_statements_only_for_known_scoped_answer() -> None:
    session = create_autospec(Session, instance=True)
    statement = make_statement()
    session.scalar.return_value = ANSWER_ID
    session.scalars.return_value = (AnswerStatementRow.from_domain(statement),)
    repository = SqlAlchemyResearchRepository(session)

    assert repository.list_statements(make_context(), ANSWER_ID) == (statement,)

    session.scalar.return_value = None
    session.scalars.reset_mock()
    assert repository.list_statements(make_context(), OTHER_ID) == ()
    session.scalars.assert_not_called()


def test_research_tables_enforce_workspace_and_traceability_relationships() -> None:
    request_table = cast(Table, ResearchRequestRow.__table__)
    answer_table = cast(Table, AnswerRow.__table__)
    statement_table = cast(Table, AnswerStatementRow.__table__)

    for table in (request_table, answer_table, statement_table):
        assert "workspace_id" in table.c
        target_sets = {
            tuple(element.target_fullname for element in constraint.elements)
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
        }
        assert ("workspaces.id",) in target_sets

    answer_targets = {
        tuple(element.target_fullname for element in constraint.elements)
        for constraint in answer_table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert ("research_requests.workspace_id", "research_requests.id") in answer_targets
    assert (
        "retrieval_runs.workspace_id",
        "retrieval_runs.research_request_id",
        "retrieval_runs.id",
    ) in answer_targets

    statement_targets = {
        tuple(element.target_fullname for element in constraint.elements)
        for constraint in statement_table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert ("answers.workspace_id", "answers.id") in statement_targets
