"""Explicit capture composition with fenced outbox dispatch; no runtime activation or provider."""

from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import Engine, select

from investment_research.infrastructure.database.outbox_unit_of_work import (
    create_outbox_delivery_unit_of_work_factory,
)
from investment_research.infrastructure.database.session import create_session_factory
from investment_research.infrastructure.database.unit_of_work import (
    create_capture_unit_of_work_factory,
)
from investment_research.infrastructure.object_storage.local import LocalObjectStorage
from investment_research.modules.delivery.application import DrainOutboxService
from investment_research.modules.delivery.dispatch import DispatchCapturedProcessingService
from investment_research.modules.delivery.domain import OutboxState
from investment_research.modules.delivery.persistence import OutboxEventRow
from investment_research.modules.delivery.ports import (
    OutboxPublicationInvalidError,
    OutboxPublicationUnavailableError,
)
from investment_research.modules.documents.application import CaptureDocumentService
from investment_research.modules.documents.contracts import PastedTextCaptureInput
from investment_research.modules.documents.persistence import OriginalArtifactRow
from investment_research.modules.processing.contracts import ProcessingJobV1
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.processing.persistence import ProcessingRunRow
from tests.integration.test_document_capture_postgresql import (
    CAPTURED_AT,
    PDF_CONTENT,
    create_workspace,
    make_capture_input,
    make_capture_service,
)
from tests.integration.test_outbox_delivery_postgresql import read_event


@pytest.mark.parametrize("media", ["pdf", "text"])
@pytest.mark.parametrize("outcome", ["published", "unavailable", "uncertain", "invalid"])
def test_explicit_capture_dispatch_preserves_original_and_owns_only_new_event(
    integration_database_engine: Engine, tmp_path: Path, media: str, outcome: str
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    # Existing due work must not be consumed by the new capture's immediate path.
    previous = make_capture_service(engine, tmp_path / "previous").capture_pdf(
        context, make_capture_input(), BytesIO(PDF_CONTENT)
    )
    sessions = create_session_factory(engine)
    with sessions() as session:
        prior = (
            session.scalars(
                select(OutboxEventRow).where(
                    OutboxEventRow.workspace_id == context.workspace_id,
                    OutboxEventRow.target_id == previous.processing_run_id,
                )
            )
            .one()
            .to_domain()
        )
    identity = Mock()
    identity.resolve.return_value = context
    publisher = Mock()
    publisher.is_ready.return_value = outcome != "unavailable"
    sent: list[ProcessingJobV1] = []

    def publish(job: ProcessingJobV1, *, timeout_seconds: float) -> None:
        assert 0 < timeout_seconds <= 2
        assert job.processing_run_id != previous.processing_run_id
        # A second connection can lock capture/run/claim immediately: no surrounding
        # capture or delivery transaction may remain open during a broker operation.
        with sessions.begin() as session:
            row = session.scalars(
                select(OutboxEventRow)
                .where(
                    OutboxEventRow.workspace_id == context.workspace_id,
                    OutboxEventRow.target_id == job.processing_run_id,
                )
                .with_for_update(nowait=True)
            ).one()
            assert row.state is OutboxState.CLAIMED and row.attempt_count == 1
            run = session.scalars(
                select(ProcessingRunRow)
                .where(
                    ProcessingRunRow.id == job.processing_run_id,
                )
                .with_for_update(nowait=True)
            ).one()
            assert run.state is ProcessingState.CAPTURED and run.attempt_count == 0
        sent.append(job)
        if outcome == "uncertain":
            raise OutboxPublicationUnavailableError("synthetic lost acknowledgement")
        if outcome == "invalid":
            raise OutboxPublicationInvalidError("synthetic invalid publication")

    publisher.publish.side_effect = publish
    delivery = DrainOutboxService(
        create_outbox_delivery_unit_of_work_factory(sessions),
        publisher,
        clock=lambda: CAPTURED_AT,
        jitter=lambda low, high: high,
    )
    dispatcher = DispatchCapturedProcessingService(delivery, identity)
    storage = LocalObjectStorage(tmp_path / "objects")
    capture = CaptureDocumentService(
        object_storage=storage,
        unit_of_work_factory=create_capture_unit_of_work_factory(sessions),
        max_upload_bytes=1024,
        processing_dispatcher=dispatcher,
        clock=lambda: CAPTURED_AT,
    )
    if media == "pdf":
        result = capture.capture_pdf(context, make_capture_input(), BytesIO(PDF_CONTENT))
    else:
        result = capture.capture_text(
            context,
            PastedTextCaptureInput(
                title="Synthetic targeted capture",
                text="Capacity expanded in Europe.",
                language="en",
            ),
        )
    assert result.state is ProcessingState.CAPTURED
    assert read_event(engine, context, prior) == prior
    with sessions() as session:
        stored = (
            session.scalars(
                select(OutboxEventRow).where(
                    OutboxEventRow.workspace_id == context.workspace_id,
                    OutboxEventRow.target_id == result.processing_run_id,
                )
            )
            .one()
            .to_domain()
        )
        run = session.get(ProcessingRunRow, result.processing_run_id)
        original = session.get(OriginalArtifactRow, result.original_artifact_id)
        assert run is not None and run.state is ProcessingState.CAPTURED and run.attempt_count == 0
        assert original is not None
        with storage.open(context, original.storage_key) as source:
            assert source.read()
    assert (
        stored.state
        is {
            "published": OutboxState.PUBLISHED,
            "unavailable": OutboxState.PENDING,
            "uncertain": OutboxState.RETRY_WAIT,
            "invalid": OutboxState.BLOCKED,
        }[outcome]
    )
    assert stored.attempt_count == (0 if outcome == "unavailable" else 1)
    assert len(sent) == (0 if outcome == "unavailable" else 1)
    if outcome in {"published", "uncertain", "invalid"}:
        dispatcher.dispatch(
            ProcessingJobV1.create(
                workspace_id=context.workspace_id,
                processing_run_id=result.processing_run_id,
            )
        )
        assert len(sent) == 1
        assert read_event(engine, context, stored) == stored
