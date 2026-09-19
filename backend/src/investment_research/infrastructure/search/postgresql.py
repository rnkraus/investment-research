"""PostgreSQL adapters for canonical lexical retrieval and dense qualification."""

from uuid import UUID

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from investment_research.modules.documents.domain import DocumentLifecycleState, SourceLocatorKind
from investment_research.modules.documents.persistence import DocumentRow, DocumentVersionRow
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.processing.persistence import (
    ChunkRow,
    ChunkSetRow,
    ProcessingRunRow,
    StructureBlockRow,
)
from investment_research.modules.search.contracts import (
    DenseQualificationRequest,
    LexicalSearchHit,
    LexicalSearchQuery,
    QualifiedDenseHit,
)


class PostgresLexicalSearch:
    """Select eligible canonical chunks through PostgreSQL full-text search."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def search(
        self,
        context: WorkspaceContext,
        query: LexicalSearchQuery,
    ) -> tuple[LexicalSearchHit, ...]:
        """Return bounded hits after all canonical eligibility predicates pass."""

        statement = self._build_statement(context, query)
        rows = self._session.execute(statement).tuples()
        return tuple(
            LexicalSearchHit(
                chunk_id=chunk_id,
                document_id=document_id,
                document_version_id=document_version_id,
                structure_block_id=structure_block_id,
                source_locator_kind=source_locator_kind,
                page_number=page_number,
                text=text,
                raw_score=float(raw_score),
            )
            for (
                chunk_id,
                document_id,
                document_version_id,
                structure_block_id,
                source_locator_kind,
                page_number,
                text,
                raw_score,
            ) in rows
        )

    @staticmethod
    def _build_statement(
        context: WorkspaceContext,
        query: LexicalSearchQuery,
    ) -> Select[tuple[UUID, UUID, UUID, UUID, SourceLocatorKind, int | None, str, float]]:
        """Build one query that filters canonical state before ranking and limiting."""

        text_query = func.websearch_to_tsquery("english", query.normalized_query)
        raw_score = func.ts_rank_cd(ChunkRow.search_vector, text_query).label("raw_score")

        statement = (
            select(
                ChunkRow.id,
                DocumentVersionRow.document_id,
                ChunkRow.document_version_id,
                ChunkRow.structure_block_id,
                StructureBlockRow.source_locator_kind,
                StructureBlockRow.page_number,
                ChunkRow.text,
                raw_score,
            )
            .select_from(ChunkRow)
            .join(
                ChunkSetRow,
                and_(
                    ChunkSetRow.workspace_id == ChunkRow.workspace_id,
                    ChunkSetRow.id == ChunkRow.chunk_set_id,
                    ChunkSetRow.processing_run_id == ChunkRow.processing_run_id,
                    ChunkSetRow.document_version_id == ChunkRow.document_version_id,
                ),
            )
            .join(
                ProcessingRunRow,
                and_(
                    ProcessingRunRow.workspace_id == ChunkRow.workspace_id,
                    ProcessingRunRow.id == ChunkRow.processing_run_id,
                    ProcessingRunRow.document_version_id == ChunkRow.document_version_id,
                ),
            )
            .join(
                StructureBlockRow,
                and_(
                    StructureBlockRow.workspace_id == ChunkRow.workspace_id,
                    StructureBlockRow.id == ChunkRow.structure_block_id,
                    StructureBlockRow.processing_run_id == ChunkRow.processing_run_id,
                    StructureBlockRow.document_version_id == ChunkRow.document_version_id,
                ),
            )
            .join(
                DocumentVersionRow,
                and_(
                    DocumentVersionRow.workspace_id == ChunkRow.workspace_id,
                    DocumentVersionRow.id == ChunkRow.document_version_id,
                ),
            )
            .join(
                DocumentRow,
                and_(
                    DocumentRow.workspace_id == DocumentVersionRow.workspace_id,
                    DocumentRow.id == DocumentVersionRow.document_id,
                    DocumentRow.current_version_id == DocumentVersionRow.id,
                ),
            )
            .where(
                ChunkRow.workspace_id == context.workspace_id,
                DocumentRow.lifecycle_state == DocumentLifecycleState.ACTIVE,
                ChunkSetRow.is_active.is_(True),
                ChunkSetRow.lexical_config_version == query.lexical_config_version,
                ProcessingRunRow.state == ProcessingState.LEXICAL_READY,
                ProcessingRunRow.lexical_config_version == query.lexical_config_version,
                ChunkRow.search_vector.bool_op("@@")(text_query),
                raw_score > 0,
            )
        )

        if query.filters.document_ids:
            statement = statement.where(DocumentRow.id.in_(query.filters.document_ids))
        if query.filters.available_at_cutoff is not None:
            statement = statement.where(
                DocumentVersionRow.available_at.is_not(None),
                DocumentVersionRow.available_at <= query.filters.available_at_cutoff,
            )
        if query.filters.captured_at_cutoff is not None:
            statement = statement.where(
                DocumentVersionRow.captured_at <= query.filters.captured_at_cutoff,
            )

        return statement.order_by(raw_score.desc(), ChunkRow.id.asc()).limit(query.candidate_limit)


class PostgresDenseCandidateQualifier:
    """Rehydrate dense provider hits only through eligible canonical state."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def qualify(
        self,
        context: WorkspaceContext,
        request: DenseQualificationRequest,
    ) -> tuple[QualifiedDenseHit, ...]:
        """Return eligible hits in provider order without disclosing rejected IDs."""

        if not request.provider_hits:
            return ()
        rows = self._session.execute(self._build_statement(context, request)).tuples()
        canonical_by_chunk_id = {
            chunk_id: (
                document_id,
                document_version_id,
                structure_block_id,
                source_locator_kind,
                page_number,
                text,
            )
            for (
                chunk_id,
                document_id,
                document_version_id,
                structure_block_id,
                source_locator_kind,
                page_number,
                text,
            ) in rows
        }
        qualified: list[QualifiedDenseHit] = []
        for provider_hit in request.provider_hits:
            canonical = canonical_by_chunk_id.get(provider_hit.chunk_id)
            if canonical is None:
                continue
            (
                document_id,
                document_version_id,
                structure_block_id,
                source_locator_kind,
                page_number,
                text,
            ) = canonical
            qualified.append(
                QualifiedDenseHit(
                    chunk_id=provider_hit.chunk_id,
                    document_id=document_id,
                    document_version_id=document_version_id,
                    structure_block_id=structure_block_id,
                    source_locator_kind=source_locator_kind,
                    page_number=page_number,
                    text=text,
                    raw_score=provider_hit.raw_score,
                )
            )
        return tuple(qualified)

    @staticmethod
    def _build_statement(
        context: WorkspaceContext,
        request: DenseQualificationRequest,
    ) -> Select[tuple[UUID, UUID, UUID, UUID, SourceLocatorKind, int | None, str]]:
        """Build one query that rejects stale, foreign, or misconfigured vector hits."""

        statement = (
            select(
                ChunkRow.id,
                DocumentVersionRow.document_id,
                ChunkRow.document_version_id,
                ChunkRow.structure_block_id,
                StructureBlockRow.source_locator_kind,
                StructureBlockRow.page_number,
                ChunkRow.text,
            )
            .select_from(ChunkRow)
            .join(
                ChunkSetRow,
                and_(
                    ChunkSetRow.workspace_id == ChunkRow.workspace_id,
                    ChunkSetRow.id == ChunkRow.chunk_set_id,
                    ChunkSetRow.processing_run_id == ChunkRow.processing_run_id,
                    ChunkSetRow.document_version_id == ChunkRow.document_version_id,
                ),
            )
            .join(
                ProcessingRunRow,
                and_(
                    ProcessingRunRow.workspace_id == ChunkRow.workspace_id,
                    ProcessingRunRow.id == ChunkRow.processing_run_id,
                    ProcessingRunRow.document_version_id == ChunkRow.document_version_id,
                ),
            )
            .join(
                StructureBlockRow,
                and_(
                    StructureBlockRow.workspace_id == ChunkRow.workspace_id,
                    StructureBlockRow.id == ChunkRow.structure_block_id,
                    StructureBlockRow.processing_run_id == ChunkRow.processing_run_id,
                    StructureBlockRow.document_version_id == ChunkRow.document_version_id,
                ),
            )
            .join(
                DocumentVersionRow,
                and_(
                    DocumentVersionRow.workspace_id == ChunkRow.workspace_id,
                    DocumentVersionRow.id == ChunkRow.document_version_id,
                ),
            )
            .join(
                DocumentRow,
                and_(
                    DocumentRow.workspace_id == DocumentVersionRow.workspace_id,
                    DocumentRow.id == DocumentVersionRow.document_id,
                    DocumentRow.current_version_id == DocumentVersionRow.id,
                ),
            )
            .where(
                ChunkRow.workspace_id == context.workspace_id,
                ChunkRow.id.in_(hit.chunk_id for hit in request.provider_hits),
                DocumentRow.lifecycle_state == DocumentLifecycleState.ACTIVE,
                ChunkSetRow.is_active.is_(True),
                ChunkSetRow.lexical_config_version == request.lexical_config_version,
                ProcessingRunRow.state == ProcessingState.SEARCHABLE,
                ProcessingRunRow.lexical_config_version == request.lexical_config_version,
                ProcessingRunRow.embedding_config_version == request.embedding_config_version,
                ProcessingRunRow.dense_index_config_version == request.dense_index_config_version,
            )
        )

        if request.filters.document_ids:
            statement = statement.where(DocumentRow.id.in_(request.filters.document_ids))
        if request.filters.available_at_cutoff is not None:
            statement = statement.where(
                DocumentVersionRow.available_at.is_not(None),
                DocumentVersionRow.available_at <= request.filters.available_at_cutoff,
            )
        if request.filters.captured_at_cutoff is not None:
            statement = statement.where(
                DocumentVersionRow.captured_at <= request.filters.captured_at_cutoff,
            )
        return statement
