"""Verify purge sequencing without a database, credentials, or live providers."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import create_autospec
from uuid import uuid4

import pytest

from investment_research.modules.documents.deletion import (
    PurgeDocumentService,
    RequestDocumentDeletionService,
)
from investment_research.modules.documents.deletion_contracts import (
    DeleteDocumentsCommand,
    DocumentDeletionReceipt,
    DocumentDeletionState,
)
from investment_research.modules.documents.deletion_ports import (
    DenseVectorPurger,
    DocumentDeletionRepository,
    DocumentDeletionUnavailableError,
    DocumentDeletionUnitOfWork,
    DocumentPurgeManifest,
    DocumentPurgePendingError,
    OriginalPurger,
)
from investment_research.modules.documents.ports import ObjectStorageError
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.application import dense_vector_id
from investment_research.modules.search.ports import SearchProviderTransientError

NOW = datetime(2026, 10, 12, tzinfo=UTC)


@dataclass
class DeletionRepositories:
    deletions: DocumentDeletionRepository


@dataclass
class RecordingUnitOfWork:
    repository: DocumentDeletionRepository
    active: bool = False

    @contextmanager
    def __call__(self) -> Iterator[DocumentDeletionUnitOfWork]:
        self.active = True
        try:
            yield DeletionRepositories(self.repository)
        finally:
            self.active = False


def test_purge_keeps_provider_calls_outside_transactions_and_batches_vectors() -> None:
    context = WorkspaceContext(user_id=uuid4(), workspace_id=uuid4())
    document_id = uuid4()
    receipt = DocumentDeletionReceipt(
        workspace_id=context.workspace_id,
        document_id=document_id,
        state=DocumentDeletionState.PENDING,
        requested_at=NOW,
    )
    repository = create_autospec(DocumentDeletionRepository, instance=True)
    factory = RecordingUnitOfWork(repository)
    repository.get_receipt.return_value = receipt
    keys = (
        f"workspaces/{context.workspace_id}/originals/one.pdf",
        f"workspaces/{context.workspace_id}/originals/two.txt",
    )
    vector_ids = tuple(
        dense_vector_id(context, chunk_id=uuid4(), embedding_config_version="v1")
        for _ in range(101)
    )
    repository.load_manifest.return_value = DocumentPurgeManifest(
        workspace_id=context.workspace_id,
        document_id=document_id,
        original_keys=keys,
        vector_ids=vector_ids,
    )
    completed = receipt.model_copy(
        update={
            "state": DocumentDeletionState.COMPLETED,
            "completed_at": NOW + timedelta(seconds=1),
        }
    )
    repository.complete.return_value = completed
    originals = create_autospec(OriginalPurger, instance=True)
    vectors = create_autospec(DenseVectorPurger, instance=True)

    def outside_transaction(*_: object) -> None:
        assert not factory.active

    originals.delete_original.side_effect = outside_transaction
    vectors.delete_vectors.side_effect = outside_transaction
    service = PurgeDocumentService(
        factory, originals, vectors, clock=lambda: NOW + timedelta(seconds=1)
    )
    assert service.purge(context, document_id) == completed
    assert [call.args[1] for call in vectors.delete_vectors.call_args_list] == [
        vector_ids[:100],
        vector_ids[100:],
    ]
    assert [call.args[1] for call in originals.delete_original.call_args_list] == list(keys)
    repository.complete.assert_called_once_with(context, document_id, completed.completed_at)


@pytest.mark.parametrize("failure", ["storage", "index", "missing_index"])
def test_failed_external_cleanup_never_finalizes_canonical_deletion(failure: str) -> None:
    context = WorkspaceContext(user_id=uuid4(), workspace_id=uuid4())
    document_id = uuid4()
    repository = create_autospec(DocumentDeletionRepository, instance=True)
    repository.get_receipt.return_value = DocumentDeletionReceipt(
        workspace_id=context.workspace_id,
        document_id=document_id,
        state=DocumentDeletionState.PENDING,
        requested_at=NOW,
    )
    repository.load_manifest.return_value = DocumentPurgeManifest(
        workspace_id=context.workspace_id,
        document_id=document_id,
        original_keys=(f"workspaces/{context.workspace_id}/originals/one.pdf",),
        vector_ids=(dense_vector_id(context, chunk_id=uuid4(), embedding_config_version="v1"),),
    )
    originals = create_autospec(OriginalPurger, instance=True)
    vectors = create_autospec(DenseVectorPurger, instance=True)
    if failure == "storage":
        originals.delete_original.side_effect = ObjectStorageError("synthetic storage failure")
    elif failure == "index":
        vectors.delete_vectors.side_effect = SearchProviderTransientError("synthetic index failure")
    with pytest.raises(DocumentPurgePendingError):
        PurgeDocumentService(
            RecordingUnitOfWork(repository),
            originals,
            None if failure == "missing_index" else vectors,
        ).purge(context, document_id)
    repository.complete.assert_not_called()


def test_completed_and_unavailable_targets_never_call_external_providers() -> None:
    context = WorkspaceContext(user_id=uuid4(), workspace_id=uuid4())
    document_id = uuid4()
    repository = create_autospec(DocumentDeletionRepository, instance=True)
    completed = DocumentDeletionReceipt(
        workspace_id=context.workspace_id,
        document_id=document_id,
        state=DocumentDeletionState.COMPLETED,
        requested_at=NOW,
        completed_at=NOW,
    )
    repository.get_receipt.return_value = completed
    originals = create_autospec(OriginalPurger, instance=True)
    service = PurgeDocumentService(RecordingUnitOfWork(repository), originals)
    assert service.purge(context, document_id) == completed
    repository.get_receipt.return_value = None
    with pytest.raises(DocumentDeletionUnavailableError):
        service.purge(context, document_id)
    originals.delete_original.assert_not_called()
    repository.load_manifest.assert_not_called()
    repository.complete.assert_not_called()


def test_request_admission_has_no_external_provider_collaborators() -> None:
    context = WorkspaceContext(user_id=uuid4(), workspace_id=uuid4())
    command = DeleteDocumentsCommand(document_ids=(uuid4(),))
    repository = create_autospec(DocumentDeletionRepository, instance=True)
    repository.request.return_value = ()
    RequestDocumentDeletionService(RecordingUnitOfWork(repository), clock=lambda: NOW).request(
        context, command
    )
    repository.request.assert_called_once_with(context, command, NOW)
