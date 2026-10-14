"""PostgreSQL adapter for the cross-module permanent-deletion aggregate."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from investment_research.infrastructure.database.outbox import SqlAlchemyOutboxRepository
from investment_research.infrastructure.database.session import (
    SessionFactory,
    transactional_session,
)
from investment_research.modules.delivery.ports import OutboxRepository
from investment_research.modules.documents.deletion_contracts import (
    DeleteDocumentsCommand,
    DocumentDeletionReceipt,
    DocumentDeletionState,
)
from investment_research.modules.documents.deletion_ports import (
    DocumentDeletionBusyError,
    DocumentDeletionUnavailableError,
    DocumentDeletionUnitOfWorkFactory,
    DocumentPurgeManifest,
    DocumentPurgePendingError,
)
from investment_research.modules.documents.domain import DocumentLifecycleState
from investment_research.modules.documents.persistence import (
    DocumentDeletionRow,
    DocumentRow,
    DocumentVersionRow,
    OriginalArtifactRow,
)
from investment_research.modules.evidence.persistence import (
    AnswerValidationRow,
    EvidenceSpanRow,
    StatementEvidenceRow,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.persistence import ProviderOperationRow
from investment_research.modules.processing.domain import (
    M0_DISABLED_EMBEDDING_CONFIG_VERSION,
    ProcessingState,
)
from investment_research.modules.processing.persistence import (
    ChunkRow,
    ChunkSetRow,
    ProcessingRunRow,
    StructureBlockRow,
)
from investment_research.modules.research.persistence import (
    AnswerRow,
    AnswerStatementRow,
    ResearchRequestRow,
)
from investment_research.modules.search.application import dense_vector_id
from investment_research.modules.search.persistence import RetrievalCandidateRow, RetrievalRunRow


class PostgresDocumentDeletionRepository:
    """Own physical dependency order here, behind application-owned deletion contracts."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._outbox: OutboxRepository = SqlAlchemyOutboxRepository(session)

    def request(
        self, context: WorkspaceContext, command: DeleteDocumentsCommand, requested_at: datetime
    ) -> tuple[DocumentDeletionReceipt, ...]:
        documents = {
            row.id: row
            for row in self._session.scalars(
                select(DocumentRow)
                .where(
                    DocumentRow.workspace_id == context.workspace_id,
                    DocumentRow.id.in_(command.document_ids),
                )
                .order_by(DocumentRow.id)
                .with_for_update()
            )
        }
        receipts = {
            row.document_id: row
            for row in self._session.scalars(
                select(DocumentDeletionRow).where(
                    DocumentDeletionRow.workspace_id == context.workspace_id,
                    DocumentDeletionRow.document_id.in_(command.document_ids),
                )
            )
        }
        if any(item not in documents and item not in receipts for item in command.document_ids):
            raise DocumentDeletionUnavailableError("document deletion is unavailable")

        versions = select(DocumentVersionRow.id).where(
            DocumentVersionRow.workspace_id == context.workspace_id,
            DocumentVersionRow.document_id.in_(command.document_ids),
        )
        busy = self._session.scalar(
            select(ProcessingRunRow.id)
            .where(
                ProcessingRunRow.workspace_id == context.workspace_id,
                ProcessingRunRow.document_version_id.in_(versions),
                ~or_(
                    # An unclaimed queued run has not started external work. The document
                    # lock and source-write guard fence any competing or delayed claim.
                    ProcessingRunRow.state == ProcessingState.CAPTURED,
                    ProcessingRunRow.state.in_(
                        [ProcessingState.SEARCHABLE, ProcessingState.FAILED]
                    ),
                    and_(
                        ProcessingRunRow.state == ProcessingState.LEXICAL_READY,
                        ProcessingRunRow.completed_at.is_not(None),
                        ProcessingRunRow.lease_id.is_(None),
                        ProcessingRunRow.lease_expires_at.is_(None),
                    ),
                ),
            )
            .limit(1)
        )
        if busy is not None:
            raise DocumentDeletionBusyError("finish document processing before deletion")

        # Validate the entire batch before changing a single target.
        if any(
            document.lifecycle_state is not DocumentLifecycleState.ACTIVE
            for document_id, document in documents.items()
            if document_id not in receipts
        ):
            raise DocumentPurgePendingError("document deletion state is inconsistent")
        for document_id in command.document_ids:
            if document_id in receipts:
                continue
            document = documents[document_id]
            receipt = DocumentDeletionRow(
                workspace_id=context.workspace_id,
                document_id=document_id,
                state=DocumentDeletionState.PENDING,
                requested_at=requested_at,
            )
            # Validate timestamps before staging the irreversible transition.
            receipt.to_contract()
            receipts[document_id] = receipt
            self._session.add(receipt)
            document.lifecycle_state = DocumentLifecycleState.PURGE_PENDING
        self._session.flush()
        return tuple(receipts[item].to_contract() for item in command.document_ids)

    def get_receipt(
        self, context: WorkspaceContext, document_id: UUID
    ) -> DocumentDeletionReceipt | None:
        row = self._session.get(DocumentDeletionRow, (context.workspace_id, document_id))
        return None if row is None else row.to_contract()

    def _pending_document(self, context: WorkspaceContext, document_id: UUID) -> DocumentRow:
        document = self._session.scalar(
            select(DocumentRow)
            .where(
                DocumentRow.workspace_id == context.workspace_id,
                DocumentRow.id == document_id,
            )
            .with_for_update()
        )
        receipt = self.get_receipt(context, document_id)
        if (
            document is None
            or document.lifecycle_state is not DocumentLifecycleState.PURGE_PENDING
            or receipt is None
            or receipt.state is not DocumentDeletionState.PENDING
        ):
            raise DocumentDeletionUnavailableError("document deletion is unavailable")
        return document

    def load_manifest(self, context: WorkspaceContext, document_id: UUID) -> DocumentPurgeManifest:
        self._pending_document(context, document_id)
        versions = select(DocumentVersionRow.id).where(
            DocumentVersionRow.workspace_id == context.workspace_id,
            DocumentVersionRow.document_id == document_id,
        )
        keys = tuple(
            self._session.scalars(
                select(OriginalArtifactRow.storage_key)
                .where(
                    OriginalArtifactRow.workspace_id == context.workspace_id,
                    OriginalArtifactRow.document_version_id.in_(versions),
                )
                .order_by(OriginalArtifactRow.storage_key)
            )
        )
        chunks = self._session.execute(
            select(ChunkRow.id, ProcessingRunRow.embedding_config_version)
            .join(
                ProcessingRunRow,
                and_(
                    ProcessingRunRow.workspace_id == ChunkRow.workspace_id,
                    ProcessingRunRow.id == ChunkRow.processing_run_id,
                ),
            )
            .where(
                ChunkRow.workspace_id == context.workspace_id,
                ChunkRow.document_version_id.in_(versions),
                ProcessingRunRow.embedding_config_version != M0_DISABLED_EMBEDDING_CONFIG_VERSION,
            )
            .order_by(ChunkRow.id)
        )
        vectors = tuple(
            dense_vector_id(context, chunk_id=chunk_id, embedding_config_version=configuration)
            for chunk_id, configuration in chunks
        )
        return DocumentPurgeManifest(
            workspace_id=context.workspace_id,
            document_id=document_id,
            original_keys=keys,
            vector_ids=vectors,
        )

    def complete(
        self, context: WorkspaceContext, document_id: UUID, completed_at: datetime
    ) -> DocumentDeletionReceipt:
        # Lock order is identical to admission, including duplicate delivery.
        document = self._session.scalar(
            select(DocumentRow)
            .where(DocumentRow.workspace_id == context.workspace_id, DocumentRow.id == document_id)
            .with_for_update()
        )
        receipt = self._session.scalar(
            select(DocumentDeletionRow)
            .where(
                DocumentDeletionRow.workspace_id == context.workspace_id,
                DocumentDeletionRow.document_id == document_id,
            )
            .with_for_update()
        )
        if receipt is not None and receipt.state is DocumentDeletionState.COMPLETED:
            return receipt.to_contract()
        if (
            document is None
            or receipt is None
            or document.lifecycle_state is not DocumentLifecycleState.PURGE_PENDING
        ):
            raise DocumentDeletionUnavailableError("document deletion is unavailable")

        version_ids = tuple(
            self._session.scalars(
                select(DocumentVersionRow.id).where(
                    DocumentVersionRow.workspace_id == context.workspace_id,
                    DocumentVersionRow.document_id == document_id,
                )
            )
        )
        run_ids = tuple(
            self._session.scalars(
                select(ProcessingRunRow.id).where(
                    ProcessingRunRow.workspace_id == context.workspace_id,
                    ProcessingRunRow.document_version_id.in_(version_ids),
                )
            )
        )
        self._remove_affected_research(context, document_id, version_ids)
        self._outbox.cancel_processing_events(context, run_ids)
        self._session.execute(
            update(ProviderOperationRow)
            .where(
                ProviderOperationRow.workspace_id == context.workspace_id,
                ProviderOperationRow.processing_run_id.in_(run_ids),
            )
            .values(owner_deleted=True, research_request_id=None, processing_run_id=None)
        )
        for model in (
            EvidenceSpanRow,
            ChunkRow,
            ChunkSetRow,
            StructureBlockRow,
            ProcessingRunRow,
            OriginalArtifactRow,
        ):
            self._session.execute(
                delete(model)
                .where(
                    model.workspace_id == context.workspace_id,
                    model.document_version_id.in_(version_ids),
                )
                .execution_options(synchronize_session=False)
            )
        self._session.execute(
            delete(DocumentVersionRow)
            .where(
                DocumentVersionRow.workspace_id == context.workspace_id,
                DocumentVersionRow.id.in_(version_ids),
            )
            .execution_options(synchronize_session=False)
        )
        self._session.execute(
            delete(DocumentRow)
            .where(
                DocumentRow.workspace_id == context.workspace_id,
                DocumentRow.id == document_id,
            )
            .execution_options(synchronize_session=False)
        )
        receipt.state = DocumentDeletionState.COMPLETED
        receipt.completed_at = completed_at
        result = receipt.to_contract()
        self._session.flush()
        return result

    def _remove_affected_research(
        self, context: WorkspaceContext, document_id: UUID, version_ids: tuple[UUID, ...]
    ) -> None:
        workspace_id = context.workspace_id
        candidates = (
            select(RetrievalRunRow.research_request_id)
            .join(
                RetrievalCandidateRow,
                and_(
                    RetrievalCandidateRow.workspace_id == RetrievalRunRow.workspace_id,
                    RetrievalCandidateRow.retrieval_run_id == RetrievalRunRow.id,
                ),
            )
            .join(
                ChunkRow,
                and_(
                    ChunkRow.workspace_id == RetrievalCandidateRow.workspace_id,
                    ChunkRow.id == RetrievalCandidateRow.chunk_id,
                ),
            )
            .where(
                RetrievalRunRow.workspace_id == workspace_id,
                ChunkRow.document_version_id.in_(version_ids),
            )
        )
        cited = (
            select(AnswerRow.research_request_id)
            .join(
                AnswerStatementRow,
                and_(
                    AnswerStatementRow.workspace_id == AnswerRow.workspace_id,
                    AnswerStatementRow.answer_id == AnswerRow.id,
                ),
            )
            .join(
                StatementEvidenceRow,
                and_(
                    StatementEvidenceRow.workspace_id == AnswerStatementRow.workspace_id,
                    StatementEvidenceRow.answer_statement_id == AnswerStatementRow.id,
                ),
            )
            .join(
                EvidenceSpanRow,
                and_(
                    EvidenceSpanRow.workspace_id == StatementEvidenceRow.workspace_id,
                    EvidenceSpanRow.id == StatementEvidenceRow.evidence_span_id,
                ),
            )
            .where(
                AnswerRow.workspace_id == workspace_id,
                EvidenceSpanRow.document_version_id.in_(version_ids),
            )
        )
        request_ids = tuple(
            self._session.scalars(
                select(ResearchRequestRow.id)
                .where(
                    ResearchRequestRow.workspace_id == workspace_id,
                    or_(
                        ResearchRequestRow.document_ids.op("@>")([document_id]),
                        ResearchRequestRow.id.in_(candidates),
                        ResearchRequestRow.id.in_(cited),
                    ),
                )
                .order_by(ResearchRequestRow.id)
                .with_for_update()
            )
        )
        answer_ids = tuple(
            self._session.scalars(
                select(AnswerRow.id).where(
                    AnswerRow.workspace_id == workspace_id,
                    AnswerRow.research_request_id.in_(request_ids),
                )
            )
        )
        statement_ids = select(AnswerStatementRow.id).where(
            AnswerStatementRow.workspace_id == workspace_id,
            AnswerStatementRow.answer_id.in_(answer_ids),
        )
        retrieval_ids = select(RetrievalRunRow.id).where(
            RetrievalRunRow.workspace_id == workspace_id,
            RetrievalRunRow.research_request_id.in_(request_ids),
        )
        self._session.execute(
            update(ProviderOperationRow)
            .where(
                ProviderOperationRow.workspace_id == workspace_id,
                ProviderOperationRow.research_request_id.in_(request_ids),
            )
            .values(owner_deleted=True, research_request_id=None, processing_run_id=None)
        )
        for model, condition in (
            (AnswerValidationRow, AnswerValidationRow.answer_id.in_(answer_ids)),
            (StatementEvidenceRow, StatementEvidenceRow.answer_statement_id.in_(statement_ids)),
            (AnswerStatementRow, AnswerStatementRow.answer_id.in_(answer_ids)),
            (AnswerRow, AnswerRow.id.in_(answer_ids)),
            (RetrievalCandidateRow, RetrievalCandidateRow.retrieval_run_id.in_(retrieval_ids)),
            (RetrievalRunRow, RetrievalRunRow.research_request_id.in_(request_ids)),
            (ResearchRequestRow, ResearchRequestRow.id.in_(request_ids)),
        ):
            self._session.execute(
                delete(model)
                .where(
                    model.workspace_id == workspace_id,
                    condition,
                )
                .execution_options(synchronize_session=False)
            )


@dataclass(frozen=True, slots=True)
class PostgresDocumentDeletionUnitOfWork:
    deletions: PostgresDocumentDeletionRepository


def create_document_deletion_unit_of_work_factory(
    session_factory: SessionFactory,
) -> DocumentDeletionUnitOfWorkFactory:
    """Bind short transactions; transient DB conflicts leave deletion pending for retry."""

    @contextmanager
    def create() -> Iterator[PostgresDocumentDeletionUnitOfWork]:
        try:
            with transactional_session(session_factory) as session:
                yield PostgresDocumentDeletionUnitOfWork(
                    PostgresDocumentDeletionRepository(session)
                )
        except SQLAlchemyError:
            raise DocumentPurgePendingError("canonical deletion could not be completed") from None

    return create
