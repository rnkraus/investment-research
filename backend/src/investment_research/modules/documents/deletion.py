"""Short canonical transactions around idempotent external document cleanup."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from investment_research.modules.documents.deletion_contracts import (
    DeleteDocumentsCommand,
    DocumentDeletionReceipt,
    DocumentDeletionState,
)
from investment_research.modules.documents.deletion_ports import (
    DENSE_VECTOR_PURGE_BATCH_LIMIT,
    DenseVectorPurger,
    DocumentDeletionUnavailableError,
    DocumentDeletionUnitOfWorkFactory,
    DocumentPurgePendingError,
    OriginalPurger,
)
from investment_research.modules.documents.ports import ObjectStorageError
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.ports import SearchProviderError

type DeletionClock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class RequestDocumentDeletionService:
    """Exclude an explicit batch immediately, without calling external providers."""

    unit_of_work_factory: DocumentDeletionUnitOfWorkFactory
    clock: DeletionClock = lambda: datetime.now(UTC)

    def request(
        self, context: WorkspaceContext, command: DeleteDocumentsCommand
    ) -> tuple[DocumentDeletionReceipt, ...]:
        with self.unit_of_work_factory() as unit_of_work:
            return unit_of_work.deletions.request(context, command, self.clock())


@dataclass(frozen=True, slots=True)
class PurgeDocumentService:
    """Keep canonical deletion intent until every store has been cleaned successfully."""

    unit_of_work_factory: DocumentDeletionUnitOfWorkFactory
    originals: OriginalPurger
    vectors: DenseVectorPurger | None = None
    clock: DeletionClock = lambda: datetime.now(UTC)

    def purge(self, context: WorkspaceContext, document_id: UUID) -> DocumentDeletionReceipt:
        with self.unit_of_work_factory() as unit_of_work:
            receipt = unit_of_work.deletions.get_receipt(context, document_id)
            if receipt is None:
                raise DocumentDeletionUnavailableError("document deletion is unavailable")
            if receipt.state is DocumentDeletionState.COMPLETED:
                return receipt
            manifest = unit_of_work.deletions.load_manifest(context, document_id)

        if manifest.workspace_id != context.workspace_id or manifest.document_id != document_id:
            raise DocumentDeletionUnavailableError("document deletion is unavailable")

        # No database transaction or connection is held during provider requests.
        try:
            if manifest.vector_ids:
                if self.vectors is None:
                    raise DocumentPurgePendingError("vector cleanup is not configured")
                for start in range(0, len(manifest.vector_ids), DENSE_VECTOR_PURGE_BATCH_LIMIT):
                    self.vectors.delete_vectors(
                        context, manifest.vector_ids[start : start + DENSE_VECTOR_PURGE_BATCH_LIMIT]
                    )
            for storage_key in manifest.original_keys:
                self.originals.delete_original(context, storage_key)
        except ObjectStorageError, SearchProviderError:
            raise DocumentPurgePendingError("external document cleanup is still pending") from None

        with self.unit_of_work_factory() as unit_of_work:
            return unit_of_work.deletions.complete(context, document_id, self.clock())
