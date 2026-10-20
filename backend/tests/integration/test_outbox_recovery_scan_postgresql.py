"""Bounded recovery snapshots and orchestration against disposable canonical state."""

from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from investment_research.infrastructure.database.outbox_unit_of_work import (
    create_outbox_delivery_unit_of_work_factory,
)
from investment_research.infrastructure.database.session import create_session_factory
from investment_research.modules.delivery.domain import DeliveryErrorCategory, OutboxState
from investment_research.modules.delivery.recovery import (
    RecoverUnstartedOutboxService,
    RecoveryStopReason,
)
from investment_research.modules.processing.persistence import ProcessingRunRow
from tests.integration.test_document_capture_postgresql import create_workspace
from tests.integration.test_outbox_delivery_postgresql import capture_events, read_event
from tests.integration.test_outbox_recovery_postgresql import RECOVER_AT, publish_event


def test_recovery_scans_twenty_then_remaining_targets_without_resetting_work(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, _ = capture_events(engine, tmp_path, count=25)
    originals = tuple(publish_event(engine, context, first=True) for _ in range(25))
    sessions = create_session_factory(engine)
    factory = create_outbox_delivery_unit_of_work_factory(sessions)
    expected = tuple(sorted(event.event_id for event in originals))
    with factory() as work:
        assert work.delivery.list_unstarted_candidates(context, now=RECOVER_AT) == expected[:20]
        # The read has no mutation or lease side effects and keeps stable tie ordering.
        assert work.delivery.list_unstarted_candidates(context, now=RECOVER_AT) == expected[:20]
    service = RecoverUnstartedOutboxService(factory, clock=lambda: RECOVER_AT)
    first = service.recover(context)
    assert (first.scanned, first.prepared) == (20, 20)
    assert first.stop_reason is RecoveryStopReason.TARGET_LIMIT
    with factory() as work:
        assert work.delivery.list_unstarted_candidates(context, now=RECOVER_AT) == expected[20:]
    second = service.recover(context)
    assert (second.scanned, second.prepared) == (5, 5)
    assert second.stop_reason is RecoveryStopReason.SNAPSHOT_EXHAUSTED
    assert service.recover(context).scanned == 0
    for original in originals:
        assert read_event(engine, context, original) == original
    with sessions() as session:
        runs = session.scalars(
            select(ProcessingRunRow).where(ProcessingRunRow.workspace_id == context.workspace_id)
        ).all()
        assert len(runs) == 25 and all(run.attempt_count == 0 for run in runs)


@pytest.mark.parametrize("successor_state", ["CLAIMED", "PUBLISHED", "BLOCKED", "CANCELLED"])
def test_newer_intent_always_suppresses_the_old_published_candidate(
    integration_database_engine: Engine, tmp_path: Path, successor_state: str
) -> None:
    engine = integration_database_engine
    context, _ = capture_events(engine, tmp_path)
    original = publish_event(engine, context, first=True)
    factory = create_outbox_delivery_unit_of_work_factory(create_session_factory(engine))
    with factory() as work:
        successor = work.delivery.recover_unstarted(
            context, event_id=original.event_id, now=RECOVER_AT
        )
        assert successor is not None
        claim = work.delivery.claim_next_due(context, now=RECOVER_AT)
        assert claim is not None and claim.lease_id is not None
        if successor_state == "PUBLISHED":
            assert work.delivery.record_published(
                context, event_id=claim.event_id, lease_id=claim.lease_id, now=RECOVER_AT
            )
        elif successor_state in {"BLOCKED", "CANCELLED"}:
            assert work.delivery.record_failure(
                context,
                event_id=claim.event_id,
                lease_id=claim.lease_id,
                now=RECOVER_AT,
                category=DeliveryErrorCategory.INVALID_JOB
                if successor_state == "BLOCKED"
                else DeliveryErrorCategory.SOURCE_INELIGIBLE,
                retry_delay_seconds=0,
            )
    with factory() as work:
        assert work.delivery.list_unstarted_candidates(context, now=RECOVER_AT) == ()
    assert read_event(engine, context, original) == original
    assert read_event(engine, context, successor).state is OutboxState(successor_state)


def test_candidate_scan_requires_timezone_aware_time(integration_database_engine: Engine) -> None:
    context = create_workspace(integration_database_engine)
    factory = create_outbox_delivery_unit_of_work_factory(
        create_session_factory(integration_database_engine)
    )
    with factory() as work, pytest.raises(ValueError, match="timezone-aware"):
        work.delivery.list_unstarted_candidates(context, now=RECOVER_AT.replace(tzinfo=None))
