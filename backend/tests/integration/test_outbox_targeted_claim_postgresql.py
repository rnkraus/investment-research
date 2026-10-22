"""Single-event claims retain the sweep's locks, scope, lifecycle, and attempt fences."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select, update

from investment_research.infrastructure.database.outbox_unit_of_work import (
    create_outbox_delivery_unit_of_work_factory,
)
from investment_research.infrastructure.database.session import create_session_factory
from investment_research.modules.delivery.domain import (
    DeliveryErrorCategory,
    OutboxEvent,
    OutboxState,
)
from investment_research.modules.delivery.persistence import OutboxEventRow
from investment_research.modules.documents.domain import DocumentLifecycleState
from investment_research.modules.documents.persistence import DocumentRow
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.processing.persistence import ProcessingRunRow
from tests.integration.test_document_capture_postgresql import create_workspace
from tests.integration.test_outbox_delivery_postgresql import NOW, capture_events, read_event
from tests.integration.test_outbox_recovery_dispatch_postgresql import prepared_target
from tests.integration.test_outbox_recovery_postgresql import RECOVER_AT


def test_targeted_claim_ignores_earlier_due_work_and_cannot_fall_back_on_repeat(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, events = capture_events(engine, tmp_path, count=3)
    target = max(events, key=lambda event: event.event_id)
    factory = create_outbox_delivery_unit_of_work_factory(create_session_factory(engine))
    with factory() as work:
        claim = work.delivery.claim_due_event(context, event_id=target.event_id, now=NOW)
    assert claim is not None and claim.event_id == target.event_id
    assert claim.state is OutboxState.CLAIMED and claim.attempt_count == 1
    with factory() as work:
        assert work.delivery.claim_due_event(context, event_id=target.event_id, now=NOW) is None
    for event in events:
        assert read_event(engine, context, event) == (claim if event == target else event)
    with create_session_factory(engine)() as session:
        run = session.get(ProcessingRunRow, target.target_id)
        assert run is not None and run.state is ProcessingState.CAPTURED and run.attempt_count == 0


def test_unknown_and_foreign_ids_leave_all_due_intent_untouched(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, (event,) = capture_events(engine, tmp_path)
    foreign = create_workspace(engine)
    factory = create_outbox_delivery_unit_of_work_factory(create_session_factory(engine))
    with factory() as work:
        assert work.delivery.claim_due_event(context, event_id=uuid4(), now=NOW) is None
        assert work.delivery.claim_due_event(foreign, event_id=event.event_id, now=NOW) is None
    assert read_event(engine, context, event) == event


def test_targeted_claim_rollback_preserves_original_intent(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, (event,) = capture_events(engine, tmp_path)
    factory = create_outbox_delivery_unit_of_work_factory(create_session_factory(engine))
    with pytest.raises(RuntimeError, match="synthetic rollback"), factory() as work:
        assert work.delivery.claim_due_event(context, event_id=event.event_id, now=NOW) is not None
        raise RuntimeError("synthetic rollback")
    assert read_event(engine, context, event) == event


def test_locked_exact_target_is_skipped_without_claiming_unrelated_work(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, events = capture_events(engine, tmp_path, count=2)
    target = events[0]
    sessions = create_session_factory(engine)
    factory = create_outbox_delivery_unit_of_work_factory(sessions)
    with sessions.begin() as session:
        session.execute(
            select(OutboxEventRow)
            .where(OutboxEventRow.event_id == target.event_id)
            .with_for_update()
        ).one()
        with factory() as work:
            assert work.delivery.claim_due_event(context, event_id=target.event_id, now=NOW) is None
    assert all(read_event(engine, context, event) == event for event in events)


def test_competing_exact_claims_have_one_owner_and_do_not_claim_another_event(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, (target, unrelated) = capture_events(engine, tmp_path, count=2)
    factory = create_outbox_delivery_unit_of_work_factory(create_session_factory(engine))
    barrier = Barrier(2)

    def compete() -> OutboxEvent | None:
        with factory() as work:
            result = work.delivery.claim_due_event(context, event_id=target.event_id, now=NOW)
            barrier.wait(timeout=10)
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result(timeout=15) for future in [pool.submit(compete), pool.submit(compete)]
        ]
    claims = [result for result in results if result is not None]
    assert len(claims) == 1 and claims[0].event_id == target.event_id
    assert claims[0].attempt_count == 1
    assert read_event(engine, context, unrelated) == unrelated


@pytest.mark.parametrize("already_started", [False, True])
def test_targeted_claim_retains_source_exclusion_and_existing_execution(
    integration_database_engine: Engine, tmp_path: Path, already_started: bool
) -> None:
    engine = integration_database_engine
    context, (event,) = capture_events(engine, tmp_path)
    sessions = create_session_factory(engine)
    with sessions.begin() as session:
        run = session.get(ProcessingRunRow, event.target_id)
        assert run is not None
        if already_started:
            session.execute(
                update(ProcessingRunRow)
                .where(ProcessingRunRow.id == run.id)
                .values(
                    state=ProcessingState.PARSING,
                    attempt_count=1,
                    started_at=NOW,
                    lease_id=uuid4(),
                    lease_expires_at=NOW + timedelta(seconds=360),
                )
            )
        else:
            session.execute(
                update(DocumentRow)
                .where(DocumentRow.current_version_id == run.document_version_id)
                .values(
                    lifecycle_state=DocumentLifecycleState.PURGE_PENDING,
                )
            )
    with sessions() as session:
        before = session.get(ProcessingRunRow, event.target_id)
        assert before is not None
        snapshot = before.to_domain()
    with create_outbox_delivery_unit_of_work_factory(sessions)() as work:
        result = work.delivery.claim_due_event(context, event_id=event.event_id, now=NOW)
    assert result is not None and result.state is OutboxState.CANCELLED
    assert result.attempt_count == 0
    with sessions() as session:
        after = session.get(ProcessingRunRow, event.target_id)
        assert after is not None and after.to_domain() == snapshot


def test_targeted_claim_preserves_due_time_fencing_and_publication_attempt_ceiling(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, (event, unrelated) = capture_events(engine, tmp_path, count=2)
    factory = create_outbox_delivery_unit_of_work_factory(create_session_factory(engine))
    for attempt in range(3):
        now = NOW + timedelta(seconds=30 * attempt)
        with factory() as work:
            claim = work.delivery.claim_due_event(context, event_id=event.event_id, now=now)
        assert claim is not None and claim.lease_id is not None
        assert claim.attempt_count == attempt + 1
        with factory() as work:
            assert work.delivery.record_failure(
                context,
                event_id=event.event_id,
                lease_id=claim.lease_id,
                now=now,
                category=DeliveryErrorCategory.DEPENDENCY_UNAVAILABLE,
                retry_delay_seconds=30,
            )
            assert work.delivery.claim_due_event(context, event_id=event.event_id, now=now) is None
    blocked = read_event(engine, context, event)
    assert blocked.state is OutboxState.BLOCKED and blocked.attempt_count == 3
    with factory() as work:
        assert (
            work.delivery.claim_due_event(
                context, event_id=event.event_id, now=NOW + timedelta(days=1)
            )
            is None
        )
    assert read_event(engine, context, unrelated) == unrelated


def test_targeted_recovery_requires_same_explicit_profile_and_cannot_select_old_generation(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, original, successor, run = prepared_target(engine, tmp_path)
    sessions = create_session_factory(engine)
    with create_outbox_delivery_unit_of_work_factory(sessions)() as work:
        assert (
            work.delivery.claim_due_event(context, event_id=successor.event_id, now=RECOVER_AT)
            is None
        )
    factory = create_outbox_delivery_unit_of_work_factory(sessions, maximum_processing_attempts=3)
    with factory() as work:
        assert (
            work.delivery.claim_due_event(context, event_id=original.event_id, now=RECOVER_AT)
            is None
        )
    assert read_event(engine, context, successor) == successor
    with factory() as work:
        claim = work.delivery.claim_due_event(context, event_id=successor.event_id, now=RECOVER_AT)
    assert claim is not None and claim.state is OutboxState.CLAIMED
    assert claim.event_id == successor.event_id and claim.attempt_count == 1
    assert read_event(engine, context, original) == original
    with sessions() as session:
        stored = session.get(ProcessingRunRow, run.id)
        assert stored is not None and stored.to_domain() == run
