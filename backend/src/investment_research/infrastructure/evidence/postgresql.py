"""PostgreSQL reader for canonical evidence-validation source state."""

from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from investment_research.modules.documents.persistence import DocumentRow, DocumentVersionRow
from investment_research.modules.evidence.contracts import (
    CanonicalEvidenceSource,
    CanonicalEvidenceSpanSource,
)
from investment_research.modules.evidence.persistence import EvidenceSpanRow, StatementEvidenceRow
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.persistence import (
    ChunkRow,
    ChunkSetRow,
    ProcessingRunRow,
    StructureBlockRow,
)
from investment_research.modules.research.domain import AnswerState, StatementSupportStatus
from investment_research.modules.research.persistence import AnswerRow, AnswerStatementRow
from investment_research.modules.search.persistence import (
    RetrievalCandidateRow,
    RetrievalRunRow,
)


class PostgresCanonicalEvidenceSpanReader:
    """Load a citation span with its current document and published-answer state."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_span_source(
        self,
        context: WorkspaceContext,
        evidence_span_id: UUID,
    ) -> CanonicalEvidenceSpanSource | None:
        """Return canonical citation state from the trusted workspace only."""

        linked_to_streamed_answer = (
            select(StatementEvidenceRow.id)
            .join(
                AnswerStatementRow,
                and_(
                    AnswerStatementRow.workspace_id == StatementEvidenceRow.workspace_id,
                    AnswerStatementRow.id == StatementEvidenceRow.answer_statement_id,
                ),
            )
            .join(
                AnswerRow,
                and_(
                    AnswerRow.workspace_id == AnswerStatementRow.workspace_id,
                    AnswerRow.id == AnswerStatementRow.answer_id,
                ),
            )
            .where(
                StatementEvidenceRow.workspace_id == EvidenceSpanRow.workspace_id,
                StatementEvidenceRow.evidence_span_id == EvidenceSpanRow.id,
                AnswerStatementRow.support_status == StatementSupportStatus.VALIDATED,
                AnswerRow.state == AnswerState.STREAMED,
            )
            .exists()
        )
        statement = (
            select(
                EvidenceSpanRow,
                DocumentVersionRow.document_id,
                DocumentRow.lifecycle_state,
                DocumentRow.current_version_id,
                linked_to_streamed_answer.label("linked_to_streamed_answer"),
            )
            .join(
                DocumentVersionRow,
                and_(
                    DocumentVersionRow.workspace_id == EvidenceSpanRow.workspace_id,
                    DocumentVersionRow.id == EvidenceSpanRow.document_version_id,
                ),
            )
            .join(
                DocumentRow,
                and_(
                    DocumentRow.workspace_id == DocumentVersionRow.workspace_id,
                    DocumentRow.id == DocumentVersionRow.document_id,
                ),
            )
            .where(
                EvidenceSpanRow.workspace_id == context.workspace_id,
                EvidenceSpanRow.id == evidence_span_id,
            )
        )
        row = self._session.execute(statement).one_or_none()
        if row is None:
            return None

        span, document_id, lifecycle_state, current_version_id, is_linked = row
        return CanonicalEvidenceSpanSource(
            span=span.to_domain(),
            document_id=document_id,
            document_lifecycle_state=lifecycle_state,
            document_current_version_id=current_version_id,
            linked_to_streamed_answer=is_linked,
        )


class PostgresCanonicalEvidenceSourceReader:
    """Reload one selected candidate and every canonical eligibility dependency."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_source(
        self,
        context: WorkspaceContext,
        retrieval_run_id: UUID,
        retrieval_candidate_id: UUID,
    ) -> CanonicalEvidenceSource | None:
        """Return scoped canonical state for one candidate in one retrieval run."""

        statement = (
            select(
                RetrievalCandidateRow,
                RetrievalRunRow,
                ChunkRow,
                ChunkSetRow,
                ProcessingRunRow,
                StructureBlockRow,
                DocumentVersionRow,
                DocumentRow,
            )
            .join(
                RetrievalRunRow,
                and_(
                    RetrievalRunRow.workspace_id == RetrievalCandidateRow.workspace_id,
                    RetrievalRunRow.id == RetrievalCandidateRow.retrieval_run_id,
                ),
            )
            .join(
                ChunkRow,
                and_(
                    ChunkRow.workspace_id == RetrievalCandidateRow.workspace_id,
                    ChunkRow.id == RetrievalCandidateRow.chunk_id,
                ),
            )
            .join(
                ChunkSetRow,
                and_(
                    ChunkSetRow.workspace_id == ChunkRow.workspace_id,
                    ChunkSetRow.id == ChunkRow.chunk_set_id,
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
                ),
            )
            .where(
                RetrievalCandidateRow.workspace_id == context.workspace_id,
                RetrievalCandidateRow.retrieval_run_id == retrieval_run_id,
                RetrievalCandidateRow.id == retrieval_candidate_id,
            )
        )
        row = self._session.execute(statement).one_or_none()
        if row is None:
            return None

        (
            candidate,
            retrieval_run,
            chunk,
            chunk_set,
            processing_run,
            structure_block,
            document_version,
            document,
        ) = row
        return CanonicalEvidenceSource(
            workspace_id=context.workspace_id,
            retrieval_candidate_id=candidate.id,
            retrieval_run_id=retrieval_run.id,
            origin=candidate.origin,
            selection_rank=candidate.selection_rank,
            chunk_id=chunk.id,
            chunk_text=chunk.text,
            chunk_set_is_active=chunk_set.is_active,
            chunk_set_lexical_config_version=chunk_set.lexical_config_version,
            processing_state=processing_run.state,
            processing_lexical_config_version=processing_run.lexical_config_version,
            processing_embedding_config_version=processing_run.embedding_config_version,
            processing_dense_index_config_version=processing_run.dense_index_config_version,
            document_id=document.id,
            document_lifecycle_state=document.lifecycle_state,
            document_current_version_id=document.current_version_id,
            document_version_id=document_version.id,
            version_available_at=document_version.available_at,
            version_captured_at=document_version.captured_at,
            structure_block_id=structure_block.id,
            source_locator_kind=structure_block.source_locator_kind,
            page_number=structure_block.page_number,
            structure_block_text=structure_block.text,
            structure_block_start_character=structure_block.start_character,
            structure_block_end_character=structure_block.end_character,
            retrieval_filters=retrieval_run.to_domain().filters,
            retrieval_mode=retrieval_run.mode,
            retrieval_lexical_config_version=retrieval_run.lexical_config_version,
            retrieval_embedding_config_version=retrieval_run.embedding_config_version,
            retrieval_dense_index_config_version=retrieval_run.dense_index_config_version,
        )
