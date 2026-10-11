"""Prove real API admission, post-commit dispatch, and surviving scoped receipts."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from investment_research.api.dependencies import (
    get_submit_document_deletion_service,
    get_workspace_context,
)
from investment_research.bootstrap import ApiContainer
from investment_research.infrastructure.database.document_deletion import (
    create_document_deletion_unit_of_work_factory,
)
from investment_research.infrastructure.database.session import create_session_factory
from investment_research.main import create_app
from investment_research.modules.documents.deletion import (
    PurgeDocumentService,
    SubmitDocumentDeletionService,
)
from investment_research.modules.documents.deletion_contracts import (
    DocumentDeletionState,
    DocumentPurgeJobV1,
)
from investment_research.modules.documents.deletion_ports import DocumentPurgeDispatchError
from investment_research.modules.documents.domain import DocumentLifecycleState, StorageKey
from investment_research.modules.documents.persistence import DocumentDeletionRow, DocumentRow
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.settings import ApiSettings
from tests.integration.factories import make_research_source, persist_research_source


@dataclass
class RecordingDispatcher:
    engine: Engine
    fail: bool = False
    jobs: list[DocumentPurgeJobV1] = field(default_factory=list)

    def dispatch(self, job: DocumentPurgeJobV1) -> None:
        # A separate connection proves canonical exclusion was committed before publication.
        with create_session_factory(self.engine)() as session:
            receipt = session.get(DocumentDeletionRow, (job.workspace_id, job.document_id))
            document = session.get(DocumentRow, job.document_id)
            assert receipt is not None and receipt.state is DocumentDeletionState.PENDING
            assert (
                document is not None
                and document.lifecycle_state is DocumentLifecycleState.PURGE_PENDING
            )
        self.jobs.append(job)
        if self.fail:
            raise DocumentPurgeDispatchError("synthetic broker unavailable")


@dataclass
class RecordingOriginalPurger:
    keys: list[StorageKey] = field(default_factory=list)

    def delete_original(self, context: WorkspaceContext, storage_key: StorageKey) -> None:
        assert storage_key.startswith(f"workspaces/{context.workspace_id}/")
        self.keys.append(storage_key)


@pytest.mark.parametrize("broker_fails", [False, True])
def test_api_commits_exclusion_before_dispatch_and_keeps_receipts_after_purge(
    integration_api_settings: ApiSettings,
    integration_database_engine: Engine,
    tmp_path: Path,
    broker_fails: bool,
) -> None:
    engine = integration_database_engine
    source = make_research_source(label="Deletion API owner")
    retained = make_research_source(label="Deletion API other workspace")
    persist_research_source(engine, source)
    persist_research_source(engine, retained)
    application = create_app(
        integration_api_settings.model_copy(update={"object_storage_root": tmp_path / "objects"})
    )
    application.dependency_overrides[get_workspace_context] = lambda: source.context
    dispatcher = RecordingDispatcher(engine, fail=broker_fails)
    payload = {"document_ids": [str(source.document.id)], "confirm_permanent": True}
    with TestClient(application) as client:
        container = cast(ApiContainer, application.state.container)
        application.dependency_overrides[get_submit_document_deletion_service] = lambda: (
            SubmitDocumentDeletionService(
                container.submit_document_deletion_service.request_service, dispatcher
            )
        )
        response = client.post("/api/v1/documents/deletions", json=payload)
        assert response.status_code == 202
        assert response.json()["dispatch_pending_document_ids"] == (
            [str(source.document.id)] if broker_fails else []
        )
        status_url = response.json()["items"][0]["status_url"]
        assert client.get(status_url).json()["state"] == "PENDING"
        assert client.get("/api/v1/documents").json()["items"] == []
        assert (
            client.get(
                f"/api/v1/documents/{source.document.id}/versions/{source.version.id}/original"
            ).status_code
            == 404
        )
        dispatcher.fail = False
        assert (
            client.post("/api/v1/documents/deletions", json=payload).json()[
                "dispatch_pending_document_ids"
            ]
            == []
        )
        assert len(dispatcher.jobs) == 2
        assert dispatcher.jobs[0] == dispatcher.jobs[1]

        originals = RecordingOriginalPurger()
        factory = create_document_deletion_unit_of_work_factory(create_session_factory(engine))
        PurgeDocumentService(factory, originals).purge(source.context, source.document.id)
        assert originals.keys == [source.original.storage_key]
        completed = client.get(status_url)
        assert completed.status_code == 200
        assert completed.json()["state"] == "COMPLETED"
        assert completed.json()["completed_at"] is not None
        assert completed.headers["cache-control"] == "private, no-store"
        assert "workspace_id" not in completed.text
        assert (
            client.post("/api/v1/documents/deletions", json=payload).json()["items"][0]["state"]
            == "COMPLETED"
        )
        assert len(dispatcher.jobs) == 2
        with create_session_factory(engine)() as session:
            assert session.get(DocumentRow, source.document.id) is None
            assert session.get(DocumentRow, retained.document.id) is not None
        application.dependency_overrides[get_workspace_context] = lambda: retained.context
        foreign_status = client.get(status_url)
        unknown_status = client.get(f"/api/v1/documents/{uuid4()}/deletion")
        assert foreign_status.status_code == unknown_status.status_code == 404
        assert (
            foreign_status.json()["error"]["message"] == unknown_status.json()["error"]["message"]
        )


@pytest.mark.parametrize("foreign_target", [False, True])
def test_api_rejects_entire_inaccessible_batch_without_publication(
    integration_api_settings: ApiSettings,
    integration_database_engine: Engine,
    tmp_path: Path,
    foreign_target: bool,
) -> None:
    engine = integration_database_engine
    source = make_research_source(label="Deletion batch owner")
    foreign = make_research_source(label="Deletion batch foreign")
    persist_research_source(engine, source)
    persist_research_source(engine, foreign)
    application = create_app(
        integration_api_settings.model_copy(update={"object_storage_root": tmp_path / "objects"})
    )
    application.dependency_overrides[get_workspace_context] = lambda: source.context
    dispatcher = RecordingDispatcher(engine)
    with TestClient(application) as client:
        container = cast(ApiContainer, application.state.container)
        application.dependency_overrides[get_submit_document_deletion_service] = lambda: (
            SubmitDocumentDeletionService(
                container.submit_document_deletion_service.request_service, dispatcher
            )
        )
        response = client.post(
            "/api/v1/documents/deletions",
            json={
                "document_ids": [
                    str(source.document.id),
                    str(foreign.document.id if foreign_target else uuid4()),
                ],
                "confirm_permanent": True,
            },
        )
    assert response.status_code == 404
    assert dispatcher.jobs == []
    with create_session_factory(engine)() as session:
        document = session.get(DocumentRow, source.document.id)
        assert document is not None and document.lifecycle_state is DocumentLifecycleState.ACTIVE
        assert (
            session.scalar(
                select(DocumentDeletionRow).where(
                    DocumentDeletionRow.workspace_id == source.context.workspace_id
                )
            )
            is None
        )
