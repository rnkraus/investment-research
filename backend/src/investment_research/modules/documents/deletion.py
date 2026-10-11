"""Short canonical transactions around idempotent external document cleanup."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from investment_research.modules.documents.deletion_contracts import (
    DeleteDocumentsCommand,
    DocumentDeletionReceipt,
    DocumentDeletionState,
    DocumentDeletionSubmission,
    DocumentPurgeJobV1,
)
from investment_research.modules.documents.deletion_ports import (
    DENSE_VECTOR_PURGE_BATCH_LIMIT,
    DenseVectorPurger,
    DocumentDeletionUnavailableError,
    DocumentDeletionUnitOfWorkFactory,
    DocumentPurgeDispatcher,
    DocumentPurgeDispatchError,
    DocumentPurgePendingError,
    OriginalPurger,
)
from investment_research.modules.documents.ports import ObjectStorageError
from investment_research.modules.identity.application import IdentityContextProvider
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
class SubmitDocumentDeletionService:
    """Publish only after the entire explicit batch commits; never undo exclusion."""

    request_service: RequestDocumentDeletionService
    dispatcher: DocumentPurgeDispatcher

    def submit(
        self, context: WorkspaceContext, command: DeleteDocumentsCommand
    ) -> DocumentDeletionSubmission:
        receipts = self.request_service.request(context, command)
        pending_dispatch: list[UUID] = []
        for receipt in receipts:
            if receipt.state is DocumentDeletionState.COMPLETED:
                continue
            try:
                self.dispatcher.dispatch(
                    DocumentPurgeJobV1.create(
                        workspace_id=context.workspace_id, document_id=receipt.document_id
                    )
                )
            except DocumentPurgeDispatchError:
                pending_dispatch.append(receipt.document_id)
        return DocumentDeletionSubmission(receipts, tuple(pending_dispatch))


@dataclass(frozen=True, slots=True)
class GetDocumentDeletionService:
    """Read a content-free receipt even after physical document removal."""

    unit_of_work_factory: DocumentDeletionUnitOfWorkFactory

    def get(self, context: WorkspaceContext, document_id: UUID) -> DocumentDeletionReceipt:
        with self.unit_of_work_factory() as unit_of_work:
            receipt = unit_of_work.deletions.get_receipt(context, document_id)
        if receipt is None:
            raise DocumentDeletionUnavailableError("document deletion is unavailable")
        return receipt


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


@dataclass(frozen=True, slots=True)
class ExecuteDocumentPurgeJobService:
    """Treat broker workspace IDs as untrusted and require recorded canonical intent."""

    purge_service: PurgeDocumentService
    identity_context_provider: IdentityContextProvider

    def execute(self, job: DocumentPurgeJobV1) -> DocumentDeletionReceipt:
        context = self.identity_context_provider.resolve()
        if job.workspace_id != context.workspace_id:
            raise DocumentDeletionUnavailableError("document deletion is unavailable")
        return self.purge_service.purge(context, job.document_id)
