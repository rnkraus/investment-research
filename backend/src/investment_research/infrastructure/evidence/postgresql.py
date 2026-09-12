"""PostgreSQL reader for canonical evidence-validation source state."""

from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from investment_research.modules.documents.persistence import DocumentRow, DocumentVersionRow
from investment_research.modules.evidence.contracts import CanonicalEvidenceSource
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.persistence import (
    ChunkRow,
    ChunkSetRow,
    ProcessingRunRow,
    StructureBlockRow,
)
from investment_research.modules.search.persistence import (
    RetrievalCandidateRow,
    RetrievalRunRow,
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
            chunk_id=chunk.id,
            chunk_text=chunk.text,
            chunk_set_is_active=chunk_set.is_active,
            chunk_set_lexical_config_version=chunk_set.lexical_config_version,
            processing_state=processing_run.state,
            processing_lexical_config_version=processing_run.lexical_config_version,
            document_id=document.id,
            document_lifecycle_state=document.lifecycle_state,
            document_current_version_id=document.current_version_id,
            document_version_id=document_version.id,
            version_available_at=document_version.available_at,
            version_captured_at=document_version.captured_at,
            structure_block_id=structure_block.id,
            page_number=structure_block.page_number,
            structure_block_text=structure_block.text,
            retrieval_filters=retrieval_run.to_domain().filters,
            retrieval_lexical_config_version=retrieval_run.lexical_config_version,
        )
