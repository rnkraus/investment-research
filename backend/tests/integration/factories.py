"""Reusable synthetic object graphs for PostgreSQL integration tests."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import Engine

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
from investment_research.modules.processing.persistence import SqlAlchemyProcessingRepository
from investment_research.modules.research.domain import (
    Answer,
    AnswerState,
    AnswerStatement,
    CoverageStatus,
    ResearchRequest,
    StatementSupportStatus,
    StatementType,
    TemporalMode,
)
from investment_research.modules.research.persistence import SqlAlchemyResearchRepository
from investment_research.modules.search.domain import (
    LEXICAL_ORDER_SELECTOR_CONFIG_VERSION,
    RetrievalCandidate,
    RetrievalFilters,
    RetrievalMode,
    RetrievalOrigin,
    RetrievalRun,
)
from investment_research.modules.search.persistence import SqlAlchemyRetrievalRepository

FIXTURE_TIME = datetime(2026, 1, 1, tzinfo=UTC)
FIXTURE_SHA256 = "c" * 64
FIXTURE_TEXTS = (
    "FY2025 revenue was $184.2 million.",
    "Operating margin increased to 18.4 percent.",
)


@dataclass(frozen=True)
class ResearchSource:
    """One complete canonical source graph available for retrieval tests."""

    context: WorkspaceContext
    workspace: Workspace
    document: Document
    version: DocumentVersion
    original: OriginalArtifact
    processing_run: ProcessingRun
    blocks: tuple[StructureBlock, ...]
    chunk_set: ChunkSet
    chunks: tuple[Chunk, ...]
    request: ResearchRequest


@dataclass(frozen=True)
class RetrievalResultFixture:
    """One completed retrieval run and its reproducible ranked candidates."""

    run: RetrievalRun
    candidates: tuple[RetrievalCandidate, ...]


@dataclass(frozen=True)
class AnswerResultFixture:
    """One answer's permitted lifecycle snapshots and final statements."""

    snapshots: tuple[Answer, ...]
    statements: tuple[AnswerStatement, ...]

    @property
    def answer(self) -> Answer:
        """Return the terminal streamed answer snapshot."""

        return self.snapshots[-1]


def make_research_source(*, label: str = "Research Source") -> ResearchSource:
    """Build a unique, internally consistent source graph without persisting it."""

    workspace_id = uuid4()
    user_id = uuid4()
    document_id = uuid4()
    version_id = uuid4()
    original_id = uuid4()
    processing_run_id = uuid4()
    chunk_set_id = uuid4()
    context = WorkspaceContext(user_id=user_id, workspace_id=workspace_id)
    workspace = Workspace(
        id=workspace_id,
        name=f"{label} {workspace_id}",
        workspace_type=WorkspaceType.SANITIZED_DEMO,
        created_at=FIXTURE_TIME,
    )
    document = Document(
        id=document_id,
        workspace_id=workspace_id,
        document_type="earnings_brief",
        lifecycle_state=DocumentLifecycleState.ACTIVE,
        current_version_id=version_id,
        created_at=FIXTURE_TIME,
    )
    version = DocumentVersion(
        id=version_id,
        workspace_id=workspace_id,
        document_id=document_id,
        version_number=1,
        title=f"Synthetic earnings brief {document_id}",
        language="en",
        content_sha256=FIXTURE_SHA256,
        published_at=FIXTURE_TIME,
        available_at=FIXTURE_TIME,
        captured_at=FIXTURE_TIME,
    )
    original = OriginalArtifact(
        id=original_id,
        workspace_id=workspace_id,
        document_version_id=version_id,
        storage_key=f"workspaces/{workspace_id}/originals/{original_id}.pdf",
        media_type=OriginalMediaType.PDF,
        byte_size=2_048,
        checksum_sha256=FIXTURE_SHA256,
        captured_at=FIXTURE_TIME,
    )
    processing_run = ProcessingRun(
        id=processing_run_id,
        workspace_id=workspace_id,
        document_version_id=version_id,
        state=ProcessingState.LEXICAL_READY,
        parser_version="synthetic-parser-1",
        parser_config_version="parser-m0.0-v1",
        chunker_version="structure-block-1",
        chunking_config_version="chunking-m0.0-v1",
        lexical_config_version="english-lexical-v1",
        embedding_config_version="m0.0-embedding-disabled-v1",
        dense_index_config_version="m0.0-dense-index-disabled-v1",
        created_at=FIXTURE_TIME,
        attempt_count=1,
        started_at=FIXTURE_TIME + timedelta(seconds=1),
        completed_at=FIXTURE_TIME + timedelta(seconds=2),
    )
    blocks = tuple(
        StructureBlock(
            id=uuid4(),
            workspace_id=workspace_id,
            processing_run_id=processing_run_id,
            document_version_id=version_id,
            source_locator_kind=SourceLocatorKind.PDF_PAGE,
            page_number=ordinal + 1,
            block_type=StructureBlockType.PARAGRAPH,
            ordinal=ordinal,
            text=text,
            start_character=0,
            end_character=len(text),
            created_at=FIXTURE_TIME + timedelta(seconds=2),
        )
        for ordinal, text in enumerate(FIXTURE_TEXTS)
    )
    chunk_set = ChunkSet(
        id=chunk_set_id,
        workspace_id=workspace_id,
        processing_run_id=processing_run_id,
        document_version_id=version_id,
        chunker_version=processing_run.chunker_version,
        chunking_config_version=processing_run.chunking_config_version,
        lexical_config_version=processing_run.lexical_config_version,
        is_active=True,
        created_at=FIXTURE_TIME + timedelta(seconds=2),
    )
    chunks = tuple(
        Chunk(
            id=uuid4(),
            workspace_id=workspace_id,
            processing_run_id=processing_run_id,
            document_version_id=version_id,
            chunk_set_id=chunk_set_id,
            structure_block_id=block.id,
            ordinal=ordinal,
            text=block.text,
            token_count=len(block.text.split()),
            created_at=FIXTURE_TIME + timedelta(seconds=2),
        )
        for ordinal, block in enumerate(blocks)
    )
    request = ResearchRequest(
        id=uuid4(),
        workspace_id=workspace_id,
        question="What was FY2025 revenue?",
        document_ids=(document_id,),
        temporal_mode=TemporalMode.CURRENT,
        created_at=FIXTURE_TIME + timedelta(seconds=3),
    )
    return ResearchSource(
        context=context,
        workspace=workspace,
        document=document,
        version=version,
        original=original,
        processing_run=processing_run,
        blocks=blocks,
        chunk_set=chunk_set,
        chunks=chunks,
        request=request,
    )


def make_searchable_research_source(
    *,
    label: str,
    primary_text: str,
    lexical_config_version: str,
    primary_page_number: int = 2,
) -> ResearchSource:
    """Build a source whose primary block is eligible for one real lexical query."""

    source = make_research_source(label=label)
    blocks = (
        source.blocks[0].model_copy(
            update={
                "page_number": primary_page_number,
                "text": primary_text,
                "end_character": len(primary_text),
            }
        ),
        source.blocks[1],
    )
    chunks = (
        source.chunks[0].model_copy(
            update={
                "text": primary_text,
                "token_count": len(primary_text.split()),
            }
        ),
        source.chunks[1],
    )
    return replace(
        source,
        processing_run=source.processing_run.model_copy(
            update={"lexical_config_version": lexical_config_version}
        ),
        blocks=blocks,
        chunk_set=source.chunk_set.model_copy(
            update={"lexical_config_version": lexical_config_version}
        ),
        chunks=chunks,
    )


def persist_workspace(engine: Engine, source: ResearchSource) -> None:
    """Persist only a source's workspace for negative relationship tests."""

    session_factory = create_session_factory(engine)
    with transactional_session(session_factory) as session:
        SqlAlchemyWorkspaceRepository(session).add(source.context, source.workspace)


def persist_research_source(engine: Engine, source: ResearchSource) -> None:
    """Persist a source graph through module repositories in dependency order."""

    session_factory = create_session_factory(engine)
    persist_workspace(engine, source)
    with transactional_session(session_factory) as session:
        SqlAlchemyDocumentRepository(session).add_capture(
            source.context,
            source.document,
            source.version,
            source.original,
        )
    with transactional_session(session_factory) as session:
        SqlAlchemyProcessingRepository(session).add_run(
            source.context,
            source.processing_run,
        )
    with transactional_session(session_factory) as session:
        SqlAlchemyProcessingRepository(session).add_lexical_output(
            source.context,
            source.blocks,
            source.chunk_set,
            source.chunks,
        )
    with transactional_session(session_factory) as session:
        SqlAlchemyResearchRepository(session).add_request(source.context, source.request)


def make_retrieval_result(source: ResearchSource) -> RetrievalResultFixture:
    """Build a completed lexical retrieval result for a persisted source."""

    started_at = FIXTURE_TIME + timedelta(seconds=4)
    completed_at = FIXTURE_TIME + timedelta(seconds=5)
    run = RetrievalRun(
        id=uuid4(),
        workspace_id=source.context.workspace_id,
        research_request_id=source.request.id,
        query_text=source.request.question,
        normalized_query="fy2025 revenue",
        filters=RetrievalFilters(document_ids=(source.document.id,)),
        mode=RetrievalMode.LEXICAL,
        lexical_config_version=source.processing_run.lexical_config_version,
        selector_config_version=LEXICAL_ORDER_SELECTOR_CONFIG_VERSION,
        lexical_candidate_limit=10,
        started_at=started_at,
        completed_at=completed_at,
    )
    candidates = tuple(
        RetrievalCandidate(
            id=uuid4(),
            workspace_id=source.context.workspace_id,
            retrieval_run_id=run.id,
            chunk_id=chunk.id,
            origin=RetrievalOrigin.LEXICAL,
            rank=rank,
            raw_score=0.75 / rank,
            fused_rank=rank,
            created_at=completed_at,
        )
        for rank, chunk in enumerate(source.chunks, start=1)
    )
    return RetrievalResultFixture(run=run, candidates=candidates)


def persist_retrieval_result(
    engine: Engine,
    source: ResearchSource,
    result: RetrievalResultFixture,
) -> None:
    """Persist a retrieval result through its workspace-scoped repository."""

    session_factory = create_session_factory(engine)
    with transactional_session(session_factory) as session:
        SqlAlchemyRetrievalRepository(session).add_result(
            source.context,
            result.run,
            result.candidates,
        )


def make_answer_result(
    source: ResearchSource,
    retrieval: RetrievalResultFixture,
) -> AnswerResultFixture:
    """Build every snapshot of one answer through its streamed terminal state."""

    answer_id = uuid4()
    created_at = FIXTURE_TIME + timedelta(seconds=6)
    snapshots = tuple(
        Answer(
            id=answer_id,
            workspace_id=source.context.workspace_id,
            research_request_id=source.request.id,
            retrieval_run_id=(
                retrieval.run.id
                if state
                in {
                    AnswerState.GENERATING,
                    AnswerState.VALIDATING,
                    AnswerState.STREAMED,
                }
                else None
            ),
            version_number=1,
            state=state,
            coverage_status=(CoverageStatus.COMPLETE if state is AnswerState.STREAMED else None),
            generator_version="fixture-generator-1",
            generation_config_version="generation-m0.0-v1",
            prompt_version="answer-prompt-v1",
            rendered_text=source.blocks[0].text if state is AnswerState.STREAMED else None,
            created_at=created_at,
            completed_at=(
                FIXTURE_TIME + timedelta(seconds=7) if state is AnswerState.STREAMED else None
            ),
        )
        for state in (
            AnswerState.REQUESTED,
            AnswerState.RETRIEVING,
            AnswerState.GENERATING,
            AnswerState.VALIDATING,
            AnswerState.STREAMED,
        )
    )
    statements = tuple(
        AnswerStatement(
            id=uuid4(),
            workspace_id=source.context.workspace_id,
            answer_id=answer_id,
            ordinal=ordinal,
            text=block.text,
            statement_type=StatementType.ARCHIVE_DERIVED,
            support_status=StatementSupportStatus.VALIDATED,
            created_at=FIXTURE_TIME + timedelta(seconds=7),
        )
        for ordinal, block in enumerate(source.blocks)
    )
    return AnswerResultFixture(snapshots=snapshots, statements=statements)


def persist_answer_result(
    engine: Engine,
    source: ResearchSource,
    result: AnswerResultFixture,
) -> None:
    """Persist an answer lifecycle and its final statements through Research."""

    session_factory = create_session_factory(engine)
    with transactional_session(session_factory) as session:
        SqlAlchemyResearchRepository(session).add_answer(source.context, result.snapshots[0])
    for snapshot in result.snapshots[1:-1]:
        with transactional_session(session_factory) as session:
            updated = SqlAlchemyResearchRepository(session).update_answer(
                source.context,
                snapshot,
            )
            if not updated:
                raise RuntimeError("synthetic answer disappeared during its lifecycle")
    with transactional_session(session_factory) as session:
        repository = SqlAlchemyResearchRepository(session)
        if not repository.update_answer(source.context, result.answer):
            raise RuntimeError("synthetic answer disappeared before its terminal state")
        repository.add_statements(source.context, result.answer, result.statements)
