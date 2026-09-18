"""Unit tests for processing lifecycle, mappings, and scoped persistence."""

from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import create_autospec
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import ForeignKeyConstraint, Table
from sqlalchemy.orm import Session

from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.domain import (
    Chunk,
    ChunkSet,
    InvalidProcessingTransitionError,
    ProcessingAttemptLimitError,
    ProcessingLeaseActiveError,
    ProcessingLeaseOwnershipError,
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
LEASE_ID = UUID("20000000-0000-4000-8000-000000000007")
OTHER_LEASE_ID = UUID("20000000-0000-4000-8000-000000000008")
CREATED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)
CLAIMED_AT = CREATED_AT + timedelta(seconds=1)
LEASE_EXPIRES_AT = CLAIMED_AT + timedelta(seconds=45)


def make_context() -> WorkspaceContext:
    """Build the deterministic trusted context used by processing tests."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)


def make_run(
    state: ProcessingState = ProcessingState.CAPTURED,
    workspace_id: UUID = WORKSPACE_ID,
    *,
    dense_indexing_enabled: bool = False,
    active_lexical_ready: bool = False,
) -> ProcessingRun:
    """Build a lifecycle-consistent processing run snapshot."""

    started_at = None if state is ProcessingState.CAPTURED else CLAIMED_AT
    is_active = state in {ProcessingState.PARSING, ProcessingState.INDEXING} or (
        state is ProcessingState.LEXICAL_READY and active_lexical_ready
    )
    completed_at = (
        CREATED_AT + timedelta(seconds=2)
        if state
        in {
            ProcessingState.LEXICAL_READY,
            ProcessingState.SEARCHABLE,
            ProcessingState.FAILED,
        }
        and not active_lexical_ready
        else None
    )
    attempt_count = 0 if state is ProcessingState.CAPTURED else 1
    lease_id = LEASE_ID if is_active else None
    lease_expires_at = LEASE_EXPIRES_AT if is_active else None
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
        embedding_config_version=(
            "openai-text-embedding-3-small-1536-v1"
            if dense_indexing_enabled
            else "m0.0-embedding-disabled-v1"
        ),
        dense_index_config_version=(
            "pinecone-serverless-cosine-1536-aws-us-east-1-v1"
            if dense_indexing_enabled
            else "m0.0-dense-index-disabled-v1"
        ),
        created_at=CREATED_AT,
        attempt_count=attempt_count,
        lease_id=lease_id,
        lease_expires_at=lease_expires_at,
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
        source_locator_kind=SourceLocatorKind.PDF_PAGE,
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
    completed_at = CREATED_AT + timedelta(seconds=2)

    parsing = captured.claim_for_processing(
        lease_id=LEASE_ID,
        claimed_at=CLAIMED_AT,
        lease_expires_at=LEASE_EXPIRES_AT,
        maximum_attempts=3,
    )
    ready = parsing.mark_lexical_ready(lease_id=LEASE_ID, completed_at=completed_at)
    failed = parsing.mark_failed(
        lease_id=LEASE_ID,
        completed_at=completed_at,
        error_summary="PDF parsing failed safely",
    )

    assert captured.state is ProcessingState.CAPTURED
    assert captured.started_at is None
    assert parsing.state is ProcessingState.PARSING
    assert parsing.started_at == CLAIMED_AT
    assert parsing.attempt_count == 1
    assert parsing.lease_id == LEASE_ID
    assert parsing.lease_expires_at == LEASE_EXPIRES_AT
    assert ready.state is ProcessingState.LEXICAL_READY
    assert ready.completed_at == completed_at
    assert ready.lease_id is None
    assert ready.lease_expires_at is None
    assert failed.state is ProcessingState.FAILED
    assert failed.error_summary == "PDF parsing failed safely"
    assert failed.lease_id is None
    assert failed.lease_expires_at is None


def test_processing_run_exposes_m1_dense_indexing_transitions() -> None:
    captured = make_run(dense_indexing_enabled=True)
    completed_at = CREATED_AT + timedelta(seconds=3)

    parsing = captured.claim_for_processing(
        lease_id=LEASE_ID,
        claimed_at=CLAIMED_AT,
        lease_expires_at=LEASE_EXPIRES_AT,
        maximum_attempts=3,
    )
    lexical_ready = parsing.mark_lexical_ready_for_indexing(lease_id=LEASE_ID)
    indexing = lexical_ready.begin_indexing(lease_id=LEASE_ID)
    searchable = indexing.mark_searchable(
        lease_id=LEASE_ID,
        completed_at=completed_at,
    )

    assert lexical_ready.state is ProcessingState.LEXICAL_READY
    assert lexical_ready.lease_id == LEASE_ID
    assert lexical_ready.completed_at is None
    assert indexing.state is ProcessingState.INDEXING
    assert indexing.lease_id == LEASE_ID
    assert searchable.state is ProcessingState.SEARCHABLE
    assert searchable.completed_at == completed_at
    assert searchable.lease_id is None
    assert searchable.lease_expires_at is None


@pytest.mark.parametrize("state", [ProcessingState.LEXICAL_READY, ProcessingState.INDEXING])
def test_processing_run_reclaims_expired_dense_stage_without_reparsing(
    state: ProcessingState,
) -> None:
    active = make_run(
        state,
        dense_indexing_enabled=True,
        active_lexical_ready=state is ProcessingState.LEXICAL_READY,
    )

    reclaimed = active.claim_for_indexing(
        lease_id=OTHER_LEASE_ID,
        claimed_at=LEASE_EXPIRES_AT,
        lease_expires_at=LEASE_EXPIRES_AT + timedelta(seconds=45),
        maximum_attempts=3,
    )

    assert reclaimed.state is state
    assert reclaimed.started_at == active.started_at
    assert reclaimed.attempt_count == 2
    assert reclaimed.lease_id == OTHER_LEASE_ID


def test_processing_run_rejects_incoherent_or_disabled_dense_configuration() -> None:
    payload = make_run().model_dump()
    payload["embedding_config_version"] = "openai-text-embedding-3-small-1536-v1"

    with pytest.raises(ValidationError, match="enabled together"):
        ProcessingRun.model_validate(payload)

    parsing = make_run(ProcessingState.PARSING)
    with pytest.raises(InvalidProcessingTransitionError, match="dense indexing disabled"):
        parsing.mark_lexical_ready_for_indexing(lease_id=LEASE_ID)


def test_processing_run_rejects_skipped_and_terminal_transitions() -> None:
    captured = make_run()
    parsing = captured.claim_for_processing(
        lease_id=LEASE_ID,
        claimed_at=CLAIMED_AT,
        lease_expires_at=LEASE_EXPIRES_AT,
        maximum_attempts=3,
    )
    ready = parsing.mark_lexical_ready(
        lease_id=LEASE_ID,
        completed_at=CREATED_AT + timedelta(seconds=2),
    )

    with pytest.raises(InvalidProcessingTransitionError, match="CAPTURED to LEXICAL_READY"):
        captured.mark_lexical_ready(
            lease_id=LEASE_ID,
            completed_at=CREATED_AT + timedelta(seconds=2),
        )
    with pytest.raises(InvalidProcessingTransitionError, match="CAPTURED to FAILED"):
        captured.mark_failed(
            lease_id=LEASE_ID,
            completed_at=CREATED_AT + timedelta(seconds=2),
            error_summary="must start first",
        )
    with pytest.raises(InvalidProcessingTransitionError, match=r"cannot claim.*LEXICAL_READY"):
        ready.claim_for_processing(
            lease_id=OTHER_LEASE_ID,
            claimed_at=CREATED_AT + timedelta(seconds=3),
            lease_expires_at=CREATED_AT + timedelta(seconds=48),
            maximum_attempts=3,
        )


def test_processing_transitions_revalidate_times_and_safe_failure_summary() -> None:
    captured = make_run()

    with pytest.raises(ValueError, match="claimed_at must be timezone-aware"):
        captured.claim_for_processing(
            lease_id=LEASE_ID,
            claimed_at=datetime(2026, 2, 20, 12),
            lease_expires_at=LEASE_EXPIRES_AT,
            maximum_attempts=3,
        )

    parsing = captured.claim_for_processing(
        lease_id=LEASE_ID,
        claimed_at=CLAIMED_AT,
        lease_expires_at=LEASE_EXPIRES_AT,
        maximum_attempts=3,
    )
    with pytest.raises(ValidationError, match="error_summary"):
        parsing.mark_failed(
            lease_id=LEASE_ID,
            completed_at=CREATED_AT + timedelta(seconds=2),
            error_summary="   ",
        )


def test_processing_run_rejects_active_duplicate_and_allows_expired_reclaim() -> None:
    parsing = make_run(ProcessingState.PARSING)

    with pytest.raises(ProcessingLeaseActiveError, match="active lease"):
        parsing.claim_for_processing(
            lease_id=OTHER_LEASE_ID,
            claimed_at=LEASE_EXPIRES_AT - timedelta(seconds=1),
            lease_expires_at=LEASE_EXPIRES_AT + timedelta(seconds=44),
            maximum_attempts=3,
        )

    reclaimed = parsing.claim_for_processing(
        lease_id=OTHER_LEASE_ID,
        claimed_at=LEASE_EXPIRES_AT,
        lease_expires_at=LEASE_EXPIRES_AT + timedelta(seconds=45),
        maximum_attempts=3,
    )

    assert reclaimed.attempt_count == 2
    assert reclaimed.started_at == parsing.started_at
    assert reclaimed.lease_id == OTHER_LEASE_ID
    assert reclaimed.lease_expires_at == LEASE_EXPIRES_AT + timedelta(seconds=45)


def test_processing_run_requires_current_lease_owner_for_terminal_transition() -> None:
    parsing = make_run(ProcessingState.PARSING)

    with pytest.raises(ProcessingLeaseOwnershipError, match="not owned"):
        parsing.mark_lexical_ready(
            lease_id=OTHER_LEASE_ID,
            completed_at=CREATED_AT + timedelta(seconds=2),
        )
    with pytest.raises(ProcessingLeaseOwnershipError, match="not owned"):
        parsing.mark_failed(
            lease_id=OTHER_LEASE_ID,
            completed_at=CREATED_AT + timedelta(seconds=2),
            error_summary="Processing failed safely",
        )


def test_processing_run_expires_owned_lease_and_fails_after_attempt_limit() -> None:
    parsing = make_run(ProcessingState.PARSING)
    expired_at = CREATED_AT + timedelta(seconds=2)
    expired = parsing.expire_lease_for_retry(
        lease_id=LEASE_ID,
        expired_at=expired_at,
    )

    assert expired.state is ProcessingState.PARSING
    assert expired.lease_id == LEASE_ID
    assert expired.lease_expires_at == expired_at

    with pytest.raises(ProcessingAttemptLimitError, match="attempt limit"):
        expired.claim_for_processing(
            lease_id=OTHER_LEASE_ID,
            claimed_at=expired_at,
            lease_expires_at=expired_at + timedelta(seconds=45),
            maximum_attempts=1,
        )

    failed = expired.mark_attempts_exhausted(
        completed_at=expired_at,
        error_summary="Processing attempt limit exceeded",
    )
    assert failed.state is ProcessingState.FAILED
    assert failed.attempt_count == 1
    assert failed.lease_id is None
    assert failed.lease_expires_at is None


def test_structure_block_preserves_exact_text_and_rejects_whitespace_only() -> None:
    block, _, _ = make_lexical_output()

    preserved = block.model_copy(update={"text": "  exact source text  "})

    assert preserved.text == "  exact source text  "
    with pytest.raises(ValidationError, match="non-whitespace"):
        StructureBlock.model_validate({**block.model_dump(), "text": " \n\t "})


def test_structure_block_accepts_exact_text_passage_locator() -> None:
    block, _, _ = make_lexical_output()
    text = "Exact 🛰 source text"

    text_block = StructureBlock.model_validate(
        {
            **block.model_dump(),
            "source_locator_kind": SourceLocatorKind.TEXT_PASSAGE,
            "page_number": None,
            "text": text,
            "start_character": 11,
            "end_character": 11 + len(text),
        }
    )

    assert text_block.source_locator_kind is SourceLocatorKind.TEXT_PASSAGE
    assert text_block.page_number is None
    assert text_block.end_character - text_block.start_character == len(text)


@pytest.mark.parametrize(
    ("changes", "expected_error"),
    [
        pytest.param(
            {"page_number": None},
            "PDF_PAGE block requires page_number",
            id="pdf-without-page",
        ),
        pytest.param(
            {"source_locator_kind": SourceLocatorKind.TEXT_PASSAGE},
            "TEXT_PASSAGE block must not define page_number",
            id="text-with-page",
        ),
        pytest.param(
            {"end_character": 10_000},
            "source offsets must match text length",
            id="wrong-source-length",
        ),
    ],
)
def test_structure_block_rejects_invalid_source_locator(
    changes: dict[str, object],
    expected_error: str,
) -> None:
    block, _, _ = make_lexical_output()

    with pytest.raises(ValidationError, match=expected_error):
        StructureBlock.model_validate({**block.model_dump(), **changes})


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

    for field_name in (
        "parser_version",
        "parser_config_version",
        "chunker_version",
        "chunking_config_version",
        "lexical_config_version",
        "embedding_config_version",
        "dense_index_config_version",
    ):
        changed_config = parsing_run.model_copy(update={field_name: "other-config"})
        with pytest.raises(ProcessingRelationshipError, match="configuration changed"):
            repository.update_run(make_context(), changed_config)


def test_repository_reports_unknown_run_without_disclosure() -> None:
    session = create_autospec(Session, instance=True)
    session.scalar.return_value = None
    repository = SqlAlchemyProcessingRepository(session)

    assert repository.get_run_by_id(make_context(), OTHER_ID) is None
    assert repository.update_run(make_context(), make_run()) is False


def test_repository_locks_scoped_run_for_lifecycle_update() -> None:
    session = create_autospec(Session, instance=True)
    stored_run = make_run()
    session.scalar.return_value = ProcessingRunRow.from_domain(stored_run)
    repository = SqlAlchemyProcessingRepository(session)

    result = repository.get_run_by_id_for_update(make_context(), PROCESSING_RUN_ID)

    assert result == stored_run
    statement = session.scalar.call_args.args[0]
    compiled = str(statement)
    assert compiled.endswith("FOR UPDATE")
    assert "processing_runs.workspace_id" in compiled
    assert "processing_runs.id" in compiled


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

    structure_table = cast(Table, StructureBlockRow.__table__)
    assert structure_table.c.source_locator_kind.nullable is False
    assert structure_table.c.page_number.nullable is True
    constraint_names = {constraint.name for constraint in structure_table.constraints}
    assert "ck_structure_blocks_valid_source_locator" in constraint_names
    assert "ck_structure_blocks_matching_text_length" in constraint_names

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
