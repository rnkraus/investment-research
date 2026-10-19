"""Canonical outbox drain integration with a deterministic provider-free publisher."""

from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import Engine, select

from investment_research.infrastructure.database.outbox_unit_of_work import (
    create_outbox_delivery_unit_of_work_factory,
)
from investment_research.infrastructure.database.session import create_session_factory
from investment_research.modules.delivery.application import DrainOutboxService, DrainStopReason
from investment_research.modules.delivery.domain import OutboxState
from investment_research.modules.delivery.persistence import OutboxEventRow
from investment_research.modules.delivery.ports import OutboxPublicationUnavailableError
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


@pytest.mark.parametrize("acknowledgement_lost", [False, True])
def test_drain_commits_before_publish_and_retains_uncertain_delivery_for_retry(
    integration_database_engine: Engine, tmp_path: Path, acknowledgement_lost: bool
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    capture = make_capture_service(engine, tmp_path / "objects").capture_pdf(
        context, make_capture_input(), BytesIO(PDF_CONTENT)
    )
    sessions = create_session_factory(engine)
    publisher = Mock()
    publisher.is_ready.return_value = True

    def publish(job: ProcessingJobV1, *, timeout_seconds: float) -> None:
        assert job.processing_run_id == capture.processing_run_id
        assert 0 < timeout_seconds <= 2
        # A different transaction can lock the claimed event immediately: the claim
        # committed and released its connection before entering this broker callback.
        with sessions.begin() as session:
            row = session.scalars(
                select(OutboxEventRow)
                .where(OutboxEventRow.workspace_id == context.workspace_id)
                .with_for_update(nowait=True)
            ).one()
            assert row.state is OutboxState.CLAIMED
            assert row.attempt_count == 1
        if acknowledgement_lost:
            raise OutboxPublicationUnavailableError("synthetic lost acknowledgement")

    publisher.publish.side_effect = publish
    service = DrainOutboxService(
        create_outbox_delivery_unit_of_work_factory(sessions),
        publisher,
        clock=lambda: CAPTURED_AT,
        jitter=lambda low, high: high,
    )
    result = service.drain(context)
    assert result.claimed == 1
    assert result.published == (0 if acknowledgement_lost else 1)
    assert result.stop_reason is (
        DrainStopReason.BROKER_UNAVAILABLE if acknowledgement_lost else DrainStopReason.EMPTY
    )
    with sessions() as session:
        row = session.scalars(
            select(OutboxEventRow).where(OutboxEventRow.workspace_id == context.workspace_id)
        ).one()
        assert row.state is (
            OutboxState.RETRY_WAIT if acknowledgement_lost else OutboxState.PUBLISHED
        )
        run = session.get(ProcessingRunRow, capture.processing_run_id)
        assert run is not None and run.state is ProcessingState.CAPTURED
        assert run.attempt_count == 0


def test_broker_not_ready_keeps_captured_intent_untouched(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    make_capture_service(engine, tmp_path / "objects").capture_pdf(
        context, make_capture_input(), BytesIO(PDF_CONTENT)
    )
    sessions = create_session_factory(engine)
    publisher = Mock()
    publisher.is_ready.return_value = False
    result = DrainOutboxService(
        create_outbox_delivery_unit_of_work_factory(sessions), publisher
    ).drain(context)
    assert result.claimed == result.scanned == 0
    publisher.publish.assert_not_called()
    with sessions() as session:
        row = session.scalars(
            select(OutboxEventRow).where(OutboxEventRow.workspace_id == context.workspace_id)
        ).one()
        assert row.state is OutboxState.PENDING
        assert row.attempt_count == 0
