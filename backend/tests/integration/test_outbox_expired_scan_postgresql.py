"""Bounded expired-work snapshots retain fences and recheck canonical changes."""

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select, update

from investment_research.infrastructure.database.outbox_unit_of_work import (
    create_outbox_delivery_unit_of_work_factory,
)
from investment_research.infrastructure.database.session import create_session_factory
from investment_research.modules.delivery.ports import ExpiredOutboxCandidate
from investment_research.modules.delivery.recovery import (
    RecoverExpiredOutboxService,
    RecoveryStopReason,
)
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.processing.persistence import ProcessingRunRow
from tests.integration.test_outbox_delivery_postgresql import NOW, capture_events, read_event
from tests.integration.test_outbox_expired_recovery_postgresql import expired_target
from tests.integration.test_outbox_recovery_postgresql import RECOVER_AT, publish_event


def test_expired_scan_prepares_twenty_then_five_without_execution_changes(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, _ = capture_events(engine, tmp_path, count=25)
    originals = tuple(publish_event(engine, context, first=True) for _ in range(25))
    sessions = create_session_factory(engine)
    snapshots = {}
    with sessions.begin() as session:
        rows = session.scalars(
            select(ProcessingRunRow).where(ProcessingRunRow.workspace_id == context.workspace_id)
        ).all()
        for row in rows:
            run = row.to_domain().claim_for_processing(
                lease_id=uuid4(),
                claimed_at=NOW,
                lease_expires_at=NOW + timedelta(seconds=360),
                maximum_attempts=3,
            )
            row.apply_state(run)
            snapshots[run.id] = run
    expected_candidates = []
    for event in sorted(originals, key=lambda event: event.event_id):
        lease_id = snapshots[event.target_id].lease_id
        assert lease_id is not None
        expected_candidates.append(ExpiredOutboxCandidate(event.event_id, lease_id))
    expected = tuple(expected_candidates)
    factory = create_outbox_delivery_unit_of_work_factory(sessions)
    with factory() as work:
        assert (
            work.delivery.list_expired_candidates(context, now=RECOVER_AT, maximum_attempts=3)
            == expected[:20]
        )
        assert work.delivery.list_unstarted_candidates(context, now=RECOVER_AT) == ()
    service = RecoverExpiredOutboxService(factory, maximum_attempts=3, clock=lambda: RECOVER_AT)
    first = service.recover(context)
    assert (first.scanned, first.prepared) == (20, 20)
    assert first.stop_reason is RecoveryStopReason.TARGET_LIMIT
    with factory() as work:
        assert (
            work.delivery.list_expired_candidates(context, now=RECOVER_AT, maximum_attempts=3)
            == expected[20:]
        )
    second = service.recover(context)
    assert (second.scanned, second.prepared) == (5, 5)
    assert second.stop_reason is RecoveryStopReason.SNAPSHOT_EXHAUSTED
    assert service.recover(context).scanned == 0
    for event in originals:
        assert read_event(engine, context, event) == event
    with sessions() as session:
        stored = session.scalars(
            select(ProcessingRunRow).where(ProcessingRunRow.workspace_id == context.workspace_id)
        ).all()
        assert len(stored) == 25
        assert {row.id: row.to_domain() for row in stored} == snapshots


def test_snapshot_token_cannot_authorize_a_later_execution(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, original, run = expired_target(engine, tmp_path)
    sessions = create_session_factory(engine)
    factory = create_outbox_delivery_unit_of_work_factory(sessions)
    with factory() as work:
        (candidate,) = work.delivery.list_expired_candidates(
            context, now=RECOVER_AT, maximum_attempts=3
        )
    new_lease_id = uuid4()
    with sessions.begin() as session:
        session.execute(
            update(ProcessingRunRow)
            .where(ProcessingRunRow.id == run.id)
            .values(
                lease_id=new_lease_id,
                attempt_count=2,
            )
        )
    with factory() as work:
        assert (
            work.delivery.recover_expired(
                context,
                event_id=candidate.event_id,
                execution_lease_id=candidate.execution_lease_id,
                now=RECOVER_AT,
                maximum_attempts=3,
            )
            is None
        )
        assert work.delivery.list_expired_candidates(
            context, now=RECOVER_AT, maximum_attempts=3
        ) == (ExpiredOutboxCandidate(original.event_id, new_lease_id),)


@pytest.mark.parametrize("dense", [False, True])
def test_completed_lexical_work_is_not_expired_indexing_work(
    integration_database_engine: Engine, tmp_path: Path, dense: bool
) -> None:
    engine = integration_database_engine
    context, _, run = expired_target(
        engine, tmp_path, ProcessingState.LEXICAL_READY if dense else ProcessingState.PARSING
    )
    assert run.lease_id is not None
    sessions = create_session_factory(engine)
    with sessions.begin() as session:
        row = session.get(ProcessingRunRow, run.id)
        assert row is not None
        if dense:
            # Historical M1 lexical completion remains terminal even with dense configuration.
            row.state = ProcessingState.LEXICAL_READY
            row.completed_at = RECOVER_AT
            row.lease_id = row.lease_expires_at = None
        else:
            row.apply_state(run.mark_lexical_ready(lease_id=run.lease_id, completed_at=RECOVER_AT))
    with create_outbox_delivery_unit_of_work_factory(sessions)() as work:
        assert (
            work.delivery.list_expired_candidates(context, now=RECOVER_AT, maximum_attempts=3) == ()
        )


def test_expired_scan_rejects_invalid_time_and_profile_limit(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, _, _ = expired_target(engine, tmp_path)
    factory = create_outbox_delivery_unit_of_work_factory(create_session_factory(engine))
    with factory() as work:
        with pytest.raises(ValueError, match="timezone-aware"):
            work.delivery.list_expired_candidates(
                context, now=RECOVER_AT.replace(tzinfo=None), maximum_attempts=3
            )
        with pytest.raises(ValueError, match="maximum_attempts must be positive"):
            work.delivery.list_expired_candidates(context, now=RECOVER_AT, maximum_attempts=0)
