"""Unit tests for processing lifecycle, mappings, and scoped persistence."""

from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import create_autospec
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import ForeignKeyConstraint, Table
from sqlalchemy.orm import Session

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.domain import (
    Chunk,
    ChunkSet,
    InvalidProcessingTransitionError,
    ProcessingRelationshipError,
    ProcessingRun,
    ProcessingScopeError,
    ProcessingState,
    StructureBlock,
    StructureBlockType,
)
from investment_research.modules.processing.persistence import (
    ChunkRow,
    ChunkSetRow,
    ProcessingRunRow,
    SqlAlchemyProcessingRepository,
    StructureBlockRow,
)
from investment_research.modules.processing.ports import ProcessingRepository

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
OTHER_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000003")
DOCUMENT_VERSION_ID = UUID("20000000-0000-4000-8000-000000000001")
PROCESSING_RUN_ID = UUID("20000000-0000-4000-8000-000000000002")
BLOCK_ID = UUID("20000000-0000-4000-8000-000000000003")
CHUNK_SET_ID = UUID("20000000-0000-4000-8000-000000000004")
CHUNK_ID = UUID("20000000-0000-4000-8000-000000000005")
OTHER_ID = UUID("20000000-0000-4000-8000-000000000006")
CREATED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)


def make_context() -> WorkspaceContext:
    """Build the deterministic trusted context used by processing tests."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)


def make_run(
    state: ProcessingState = ProcessingState.CAPTURED,
    workspace_id: UUID = WORKSPACE_ID,
) -> ProcessingRun:
    """Build a lifecycle-consistent processing run snapshot."""

    started_at = None if state is ProcessingState.CAPTURED else CREATED_AT + timedelta(seconds=1)
    completed_at = (
        CREATED_AT + timedelta(seconds=2)
        if state in {ProcessingState.LEXICAL_READY, ProcessingState.FAILED}
        else None
    )
    error_summary = "Synthetic parser failure" if state is ProcessingState.FAILED else None
    return ProcessingRun(
        id=PROCESSING_RUN_ID,
        workspace_id=workspace_id,
        document_version_id=DOCUMENT_VERSION_ID,
        state=state,
        parser_version="pymupdf-1",
        parser_config_version="parser-m0.0-v1",
        chunker_version="structure-block-1",
        chunking_config_version="chunking-m0.0-v1",
        lexical_config_version="english-lexical-v1",
        created_at=CREATED_AT,
        started_at=started_at,
        completed_at=completed_at,
        error_summary=error_summary,
    )


def make_lexical_output() -> tuple[StructureBlock, ChunkSet, Chunk]:
    """Build one consistent block, chunk set, and chunk."""

    text = "FY2025 revenue was $184.2 million."
    block = StructureBlock(
        id=BLOCK_ID,
        workspace_id=WORKSPACE_ID,
        processing_run_id=PROCESSING_RUN_ID,
        document_version_id=DOCUMENT_VERSION_ID,
        page_number=2,
        block_type=StructureBlockType.PARAGRAPH,
        ordinal=0,
        text=text,
        start_character=0,
        end_character=len(text),
        created_at=CREATED_AT,
    )
    chunk_set = ChunkSet(
        id=CHUNK_SET_ID,
        workspace_id=WORKSPACE_ID,
        processing_run_id=PROCESSING_RUN_ID,
        document_version_id=DOCUMENT_VERSION_ID,
        chunker_version="structure-block-1",
        chunking_config_version="chunking-m0.0-v1",
        lexical_config_version="english-lexical-v1",
        is_active=True,
        created_at=CREATED_AT,
    )
    chunk = Chunk(
        id=CHUNK_ID,
        workspace_id=WORKSPACE_ID,
        processing_run_id=PROCESSING_RUN_ID,
        document_version_id=DOCUMENT_VERSION_ID,
        chunk_set_id=CHUNK_SET_ID,
        structure_block_id=BLOCK_ID,
        ordinal=0,
        text=text,
        token_count=8,
        created_at=CREATED_AT,
    )
    return block, chunk_set, chunk


@pytest.mark.parametrize(
    ("state", "started_at", "completed_at", "error_summary", "expected_error"),
    [
        pytest.param(
            ProcessingState.CAPTURED,
            CREATED_AT,
            None,
            None,
            "CAPTURED",
            id="captured-with-start",
        ),
        pytest.param(
            ProcessingState.PARSING,
            None,
            None,
            None,
            "PARSING",
            id="parsing-without-start",
        ),
        pytest.param(
            ProcessingState.LEXICAL_READY,
            CREATED_AT,
            None,
            None,
            "terminal processing state",
            id="ready-without-completion",
        ),
        pytest.param(
            ProcessingState.FAILED,
            CREATED_AT,
            CREATED_AT,
            None,
            "error_summary",
            id="failed-without-error",
        ),
    ],
)
def test_processing_run_rejects_inconsistent_lifecycle(
    state: ProcessingState,
    started_at: datetime | None,
    completed_at: datetime | None,
    error_summary: str | None,
    expected_error: str,
) -> None:
    payload = make_run().model_dump()
    payload.update(
        state=state,
        started_at=started_at,
        completed_at=completed_at,
        error_summary=error_summary,
    )

    with pytest.raises(ValidationError, match=expected_error):
        ProcessingRun.model_validate(payload)


def test_processing_run_exposes_valid_immutable_forward_transitions() -> None:
    captured = make_run()
    started_at = CREATED_AT + timedelta(seconds=1)
    completed_at = CREATED_AT + timedelta(seconds=2)

    parsing = captured.start_parsing(started_at=started_at)
    ready = parsing.mark_lexical_ready(completed_at=completed_at)
    failed = parsing.mark_failed(
        completed_at=completed_at,
        error_summary="PDF parsing failed safely",
    )

    assert captured.state is ProcessingState.CAPTURED
    assert captured.started_at is None
    assert parsing.state is ProcessingState.PARSING
    assert parsing.started_at == started_at
    assert ready.state is ProcessingState.LEXICAL_READY
    assert ready.completed_at == completed_at
    assert failed.state is ProcessingState.FAILED
    assert failed.error_summary == "PDF parsing failed safely"


def test_processing_run_rejects_skipped_and_terminal_transitions() -> None:
    captured = make_run()
    parsing = captured.start_parsing(started_at=CREATED_AT + timedelta(seconds=1))
    ready = parsing.mark_lexical_ready(completed_at=CREATED_AT + timedelta(seconds=2))

    with pytest.raises(InvalidProcessingTransitionError, match="CAPTURED to LEXICAL_READY"):
        captured.mark_lexical_ready(completed_at=CREATED_AT + timedelta(seconds=2))
    with pytest.raises(InvalidProcessingTransitionError, match="CAPTURED to FAILED"):
        captured.mark_failed(
            completed_at=CREATED_AT + timedelta(seconds=2),
            error_summary="must start first",
        )
    with pytest.raises(InvalidProcessingTransitionError, match="LEXICAL_READY to PARSING"):
        ready.start_parsing(started_at=CREATED_AT + timedelta(seconds=3))


def test_processing_transitions_revalidate_times_and_safe_failure_summary() -> None:
    captured = make_run()

    with pytest.raises(ValidationError) as error:
        captured.start_parsing(started_at=datetime(2026, 2, 20, 12))
    assert error.value.errors()[0]["type"] == "timezone_aware"

    parsing = captured.start_parsing(started_at=CREATED_AT + timedelta(seconds=1))
    with pytest.raises(ValidationError, match="error_summary"):
        parsing.mark_failed(
            completed_at=CREATED_AT + timedelta(seconds=2),
            error_summary="   ",
        )


def test_structure_block_preserves_exact_text_and_rejects_whitespace_only() -> None:
    block, _, _ = make_lexical_output()

    preserved = block.model_copy(update={"text": "  exact source text  "})

    assert preserved.text == "  exact source text  "
    with pytest.raises(ValidationError, match="non-whitespace"):
        StructureBlock.model_validate({**block.model_dump(), "text": " \n\t "})


def test_repository_matches_application_owned_protocol() -> None:
    session = create_autospec(Session, instance=True)

    repository: ProcessingRepository = SqlAlchemyProcessingRepository(session)

    assert isinstance(repository, SqlAlchemyProcessingRepository)


def test_repository_stages_run_only_in_matching_scope() -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyProcessingRepository(session)
    run = make_run()

    repository.add_run(make_context(), run)

    session.add.assert_called_once()
    staged_row = session.add.call_args.args[0]
    assert isinstance(staged_row, ProcessingRunRow)
    assert staged_row.to_domain() == run

    with pytest.raises(ProcessingScopeError, match="outside the active workspace"):
        repository.add_run(make_context(), make_run(workspace_id=OTHER_WORKSPACE_ID))


def test_repository_updates_only_mutable_run_state() -> None:
    session = create_autospec(Session, instance=True)
    stored_row = ProcessingRunRow.from_domain(make_run())
    session.scalar.return_value = stored_row
    repository = SqlAlchemyProcessingRepository(session)
    parsing_run = make_run(ProcessingState.PARSING)

    updated = repository.update_run(make_context(), parsing_run)

    assert updated is True
    assert stored_row.to_domain() == parsing_run

    changed_config = parsing_run.model_copy(update={"parser_version": "other-parser"})
    with pytest.raises(ProcessingRelationshipError, match="configuration changed"):
        repository.update_run(make_context(), changed_config)


def test_repository_reports_unknown_run_without_disclosure() -> None:
    session = create_autospec(Session, instance=True)
    session.scalar.return_value = None
    repository = SqlAlchemyProcessingRepository(session)

    assert repository.get_run_by_id(make_context(), OTHER_ID) is None
    assert repository.update_run(make_context(), make_run()) is False


def test_repository_stages_consistent_lexical_output() -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyProcessingRepository(session)
    block, chunk_set, chunk = make_lexical_output()

    repository.add_lexical_output(make_context(), [block], chunk_set, [chunk])

    assert session.add_all.call_count == 2
    block_row, chunk_set_row = session.add_all.call_args_list[0].args[0]
    (chunk_row,) = session.add_all.call_args_list[1].args[0]
    assert isinstance(block_row, StructureBlockRow)
    assert isinstance(chunk_set_row, ChunkSetRow)
    assert isinstance(chunk_row, ChunkRow)
    session.flush.assert_called_once_with()


def test_repository_rejects_inconsistent_lexical_output() -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyProcessingRepository(session)
    block, chunk_set, chunk = make_lexical_output()
    mismatched_chunk = chunk.model_copy(update={"structure_block_id": OTHER_ID})

    with pytest.raises(ProcessingRelationshipError, match="source blocks"):
        repository.add_lexical_output(make_context(), [block], chunk_set, [mismatched_chunk])

    session.add_all.assert_not_called()


def test_processing_tables_enforce_scope_and_lexical_indexes() -> None:
    tables = (
        cast(Table, ProcessingRunRow.__table__),
        cast(Table, StructureBlockRow.__table__),
        cast(Table, ChunkSetRow.__table__),
        cast(Table, ChunkRow.__table__),
    )

    for table in tables:
        assert "workspace_id" in table.c
        target_sets = {
            tuple(element.target_fullname for element in constraint.elements)
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
        }
        assert ("workspaces.id",) in target_sets

    chunk_table = cast(Table, ChunkRow.__table__)
    computed = chunk_table.c.search_vector.computed
    assert computed is not None
    assert computed.persisted is True
    assert "to_tsvector('english'::regconfig, text)" in str(computed.sqltext)

    chunk_index = next(
        index for index in chunk_table.indexes if index.name == "ix_chunks_search_vector"
    )
    assert chunk_index.dialect_options["postgresql"]["using"] == "gin"

    chunk_set_table = cast(Table, ChunkSetRow.__table__)
    active_index = next(
        index for index in chunk_set_table.indexes if index.name == "ux_chunk_sets_active_profile"
    )
    assert active_index.unique is True
    assert active_index.dialect_options["postgresql"]["where"] is not None
