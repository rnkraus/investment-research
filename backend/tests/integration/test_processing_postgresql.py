"""PostgreSQL integration tests for scoped lexical processing persistence."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError

from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.modules.documents.domain import (
    Document,
    DocumentLifecycleState,
    DocumentVersion,
    OriginalArtifact,
    OriginalMediaType,
    SourceLocatorKind,
)
from investment_research.modules.documents.persistence import SqlAlchemyDocumentRepository
from investment_research.modules.identity.domain import (
    Workspace,
    WorkspaceContext,
    WorkspaceType,
)
from investment_research.modules.identity.persistence import SqlAlchemyWorkspaceRepository
from investment_research.modules.processing.domain import (
    Chunk,
    ChunkSet,
    ProcessingRun,
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

FIXTURE_TIME = datetime(2026, 1, 1, tzinfo=UTC)
FIXTURE_SHA256 = "b" * 64
FIXTURE_TEXT = "FY2025 revenue was $184.2 million."


def make_workspace() -> tuple[WorkspaceContext, Workspace]:
    """Create one unique synthetic workspace and trusted context."""

    workspace_id = uuid4()
    return (
        WorkspaceContext(user_id=uuid4(), workspace_id=workspace_id),
        Workspace(
            id=workspace_id,
            name=f"Processing Workspace {workspace_id}",
            workspace_type=WorkspaceType.SANITIZED_DEMO,
            created_at=FIXTURE_TIME,
        ),
    )


def make_capture(
    context: WorkspaceContext,
) -> tuple[Document, DocumentVersion, OriginalArtifact]:
    """Create one valid document source for a processing run."""

    document_id = uuid4()
    version_id = uuid4()
    original_id = uuid4()
    return (
        Document(
            id=document_id,
            workspace_id=context.workspace_id,
            document_type="earnings_brief",
            lifecycle_state=DocumentLifecycleState.ACTIVE,
            current_version_id=version_id,
            created_at=FIXTURE_TIME,
        ),
        DocumentVersion(
            id=version_id,
            workspace_id=context.workspace_id,
            document_id=document_id,
            version_number=1,
            title=f"Processing source {document_id}",
            language="en",
            content_sha256=FIXTURE_SHA256,
            published_at=FIXTURE_TIME,
            available_at=FIXTURE_TIME,
            captured_at=FIXTURE_TIME,
        ),
        OriginalArtifact(
            id=original_id,
            workspace_id=context.workspace_id,
            document_version_id=version_id,
            storage_key=f"workspaces/{context.workspace_id}/originals/{original_id}.pdf",
            media_type=OriginalMediaType.PDF,
            byte_size=2_048,
            checksum_sha256=FIXTURE_SHA256,
            captured_at=FIXTURE_TIME,
        ),
    )


def make_run(
    context: WorkspaceContext,
    document_version_id: UUID,
    *,
    run_id: UUID | None = None,
    state: ProcessingState = ProcessingState.CAPTURED,
    dense_indexing_enabled: bool = False,
    active_lexical_ready: bool = False,
) -> ProcessingRun:
    """Create a lifecycle-consistent processing snapshot."""

    started_at = None if state is ProcessingState.CAPTURED else FIXTURE_TIME + timedelta(seconds=1)
    is_active = state in {ProcessingState.PARSING, ProcessingState.INDEXING} or (
        state is ProcessingState.LEXICAL_READY and active_lexical_ready
    )
    completed_at = (
        FIXTURE_TIME + timedelta(seconds=2)
        if state
        in {
            ProcessingState.LEXICAL_READY,
            ProcessingState.SEARCHABLE,
            ProcessingState.FAILED,
        }
        and not active_lexical_ready
        else None
    )
    is_captured = state is ProcessingState.CAPTURED
    return ProcessingRun(
        id=run_id or uuid4(),
        workspace_id=context.workspace_id,
        document_version_id=document_version_id,
        state=state,
        parser_version="synthetic-parser-1",
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
        created_at=FIXTURE_TIME,
        attempt_count=0 if is_captured else 1,
        lease_id=uuid4() if is_active else None,
        lease_expires_at=(FIXTURE_TIME + timedelta(seconds=46) if is_active else None),
        started_at=started_at,
        completed_at=completed_at,
    )


def make_lexical_output(
    context: WorkspaceContext,
    run: ProcessingRun,
) -> tuple[StructureBlock, ChunkSet, Chunk]:
    """Create one consistent block, active chunk set, and lexical chunk."""

    block_id = uuid4()
    chunk_set_id = uuid4()
    block = StructureBlock(
        id=block_id,
        workspace_id=context.workspace_id,
        processing_run_id=run.id,
        document_version_id=run.document_version_id,
        source_locator_kind=SourceLocatorKind.PDF_PAGE,
        page_number=1,
        block_type=StructureBlockType.PARAGRAPH,
        ordinal=0,
        text=FIXTURE_TEXT,
        start_character=0,
        end_character=len(FIXTURE_TEXT),
        created_at=FIXTURE_TIME,
    )
    chunk_set = ChunkSet(
        id=chunk_set_id,
        workspace_id=context.workspace_id,
        processing_run_id=run.id,
        document_version_id=run.document_version_id,
        chunker_version=run.chunker_version,
        chunking_config_version=run.chunking_config_version,
        lexical_config_version=run.lexical_config_version,
        is_active=True,
        created_at=FIXTURE_TIME,
    )
    chunk = Chunk(
        id=uuid4(),
        workspace_id=context.workspace_id,
        processing_run_id=run.id,
        document_version_id=run.document_version_id,
        chunk_set_id=chunk_set_id,
        structure_block_id=block_id,
        ordinal=0,
        text=FIXTURE_TEXT,
        token_count=8,
        created_at=FIXTURE_TIME,
    )
    return block, chunk_set, chunk


def persist_workspace_and_capture(
    engine: Engine,
    context: WorkspaceContext,
    workspace: Workspace,
    capture: tuple[Document, DocumentVersion, OriginalArtifact],
) -> None:
    """Persist prerequisites in the same order as the application workflow."""

    session_factory = create_session_factory(engine)
    with transactional_session(session_factory) as session:
        SqlAlchemyWorkspaceRepository(session).add(context, workspace)
    with transactional_session(session_factory) as session:
        SqlAlchemyDocumentRepository(session).add_capture(context, *capture)


def test_processing_lifecycle_and_lexical_output_are_scoped_and_searchable(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    context, workspace = make_workspace()
    foreign_context, foreign_workspace = make_workspace()
    capture = make_capture(context)
    persist_workspace_and_capture(integration_database_engine, context, workspace, capture)
    with transactional_session(session_factory) as session:
        SqlAlchemyWorkspaceRepository(session).add(foreign_context, foreign_workspace)

    version = capture[1]
    captured_run = make_run(context, version.id)
    parsing_run = make_run(
        context,
        version.id,
        run_id=captured_run.id,
        state=ProcessingState.PARSING,
    )
    ready_run = make_run(
        context,
        version.id,
        run_id=captured_run.id,
        state=ProcessingState.LEXICAL_READY,
    )
    block, chunk_set, chunk = make_lexical_output(context, ready_run)
    second_block = block.model_copy(update={"id": uuid4(), "ordinal": 1})
    second_chunk = chunk.model_copy(
        update={
            "id": uuid4(),
            "structure_block_id": second_block.id,
            "ordinal": 1,
        }
    )

    with transactional_session(session_factory) as session:
        SqlAlchemyProcessingRepository(session).add_run(context, captured_run)
    with transactional_session(session_factory) as session:
        assert SqlAlchemyProcessingRepository(session).update_run(context, parsing_run) is True
    with transactional_session(session_factory) as session:
        repository = SqlAlchemyProcessingRepository(session)
        assert repository.update_run(context, ready_run) is True
        repository.add_lexical_output(
            context,
            [second_block, block],
            chunk_set,
            [second_chunk, chunk],
        )

    with transactional_session(session_factory) as session:
        repository = SqlAlchemyProcessingRepository(session)
        assert repository.get_run_by_id(context, ready_run.id) == ready_run
        assert repository.get_run_by_id(foreign_context, ready_run.id) is None
        assert session.get(StructureBlockRow, block.id) is not None
        assert session.get(ChunkSetRow, chunk_set.id) is not None
        assert session.get(ChunkRow, chunk.id) is not None
        assert repository.list_active_chunks(context, ready_run.id) == (
            chunk,
            second_chunk,
        )
        assert repository.list_active_chunks(foreign_context, ready_run.id) == ()
        assert repository.list_active_chunks(context, uuid4()) == ()
        lexical_match_count = session.scalar(
            select(func.count())
            .select_from(ChunkRow)
            .where(
                ChunkRow.id == chunk.id,
                ChunkRow.search_vector.bool_op("@@")(func.plainto_tsquery("english", "revenue")),
            )
        )
        assert lexical_match_count == 1


def test_m1_dense_indexing_lifecycle_is_persisted_and_database_guarded(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    context, workspace = make_workspace()
    capture = make_capture(context)
    persist_workspace_and_capture(integration_database_engine, context, workspace, capture)

    captured = make_run(
        context,
        capture[1].id,
        dense_indexing_enabled=True,
    )
    lease_id = uuid4()
    parsing = captured.claim_for_processing(
        lease_id=lease_id,
        claimed_at=FIXTURE_TIME + timedelta(seconds=1),
        lease_expires_at=FIXTURE_TIME + timedelta(seconds=46),
        maximum_attempts=3,
    )
    lexical_ready = parsing.mark_lexical_ready_for_indexing(lease_id=lease_id)
    indexing = lexical_ready.begin_indexing(lease_id=lease_id)
    searchable = indexing.mark_searchable(
        lease_id=lease_id,
        completed_at=FIXTURE_TIME + timedelta(seconds=2),
    )

    with transactional_session(session_factory) as session:
        SqlAlchemyProcessingRepository(session).add_run(context, captured)
    for snapshot in (parsing, lexical_ready, indexing, searchable):
        with transactional_session(session_factory) as session:
            assert SqlAlchemyProcessingRepository(session).update_run(context, snapshot) is True

    with transactional_session(session_factory) as session:
        stored = SqlAlchemyProcessingRepository(session).get_run_by_id(context, captured.id)
    assert stored == searchable

    invalid_row = ProcessingRunRow.from_domain(indexing)
    invalid_row.id = uuid4()
    invalid_row.embedding_config_version = "m0.0-embedding-disabled-v1"
    invalid_row.dense_index_config_version = "m0.0-dense-index-disabled-v1"
    with (
        pytest.raises(IntegrityError),
        transactional_session(session_factory) as session,
    ):
        session.add(invalid_row)


def test_database_rejects_processing_run_linked_across_workspaces(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    source_context, source_workspace = make_workspace()
    foreign_context, foreign_workspace = make_workspace()
    capture = make_capture(source_context)
    persist_workspace_and_capture(
        integration_database_engine,
        source_context,
        source_workspace,
        capture,
    )
    with transactional_session(session_factory) as session:
        SqlAlchemyWorkspaceRepository(session).add(foreign_context, foreign_workspace)

    cross_workspace_run = make_run(foreign_context, capture[1].id)

    with (
        pytest.raises(IntegrityError),
        transactional_session(session_factory) as session,
    ):
        SqlAlchemyProcessingRepository(session).add_run(foreign_context, cross_workspace_run)


def test_database_allows_only_one_active_chunk_profile(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    context, workspace = make_workspace()
    capture = make_capture(context)
    persist_workspace_and_capture(integration_database_engine, context, workspace, capture)
    run = make_run(context, capture[1].id, state=ProcessingState.LEXICAL_READY)
    block, chunk_set, chunk = make_lexical_output(context, run)

    with transactional_session(session_factory) as session:
        repository = SqlAlchemyProcessingRepository(session)
        repository.add_run(context, run)
    with transactional_session(session_factory) as session:
        SqlAlchemyProcessingRepository(session).add_lexical_output(
            context,
            [block],
            chunk_set,
            [chunk],
        )

    duplicate_active_profile = chunk_set.model_copy(update={"id": uuid4()})
    with (
        pytest.raises(IntegrityError),
        transactional_session(session_factory) as session,
    ):
        session.add(ChunkSetRow.from_domain(duplicate_active_profile))


def test_database_enforces_media_appropriate_structure_block_locator(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    context, workspace = make_workspace()
    document, version, original = make_capture(context)
    text_original = OriginalArtifact.model_validate(
        {
            **original.model_dump(),
            "storage_key": original.storage_key.removesuffix(".pdf") + ".txt",
            "media_type": OriginalMediaType.PLAIN_TEXT,
        }
    )
    capture = (document, version, text_original)
    persist_workspace_and_capture(integration_database_engine, context, workspace, capture)
    run = make_run(context, capture[1].id, state=ProcessingState.LEXICAL_READY)

    with transactional_session(session_factory) as session:
        SqlAlchemyProcessingRepository(session).add_run(context, run)

    text = "Exact pasted-text passage."
    text_block = StructureBlock(
        id=uuid4(),
        workspace_id=context.workspace_id,
        processing_run_id=run.id,
        document_version_id=run.document_version_id,
        source_locator_kind=SourceLocatorKind.TEXT_PASSAGE,
        page_number=None,
        block_type=StructureBlockType.PARAGRAPH,
        ordinal=0,
        text=text,
        start_character=7,
        end_character=7 + len(text),
        created_at=FIXTURE_TIME,
    )
    with transactional_session(session_factory) as session:
        session.add(StructureBlockRow.from_domain(text_block))

    invalid_row = StructureBlockRow.from_domain(text_block)
    invalid_row.id = uuid4()
    invalid_row.ordinal = 1
    invalid_row.page_number = 1
    with (
        pytest.raises(IntegrityError),
        transactional_session(session_factory) as session,
    ):
        session.add(invalid_row)
