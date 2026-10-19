"""Delivery concurrency, scope, and fencing in disposable PostgreSQL only."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select, update

from investment_research.infrastructure.database.outbox import SqlAlchemyOutboxRepository
from investment_research.infrastructure.database.outbox_delivery import (
    SqlAlchemyOutboxDeliveryRepository,
)
from investment_research.infrastructure.database.outbox_unit_of_work import (
    create_outbox_delivery_unit_of_work_factory,
)
from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.modules.delivery.domain import (
    DeliveryErrorCategory,
    OutboxEvent,
    OutboxState,
    OutboxTransitionError,
)
from investment_research.modules.delivery.persistence import OutboxEventRow
from investment_research.modules.documents.domain import DocumentLifecycleState
from investment_research.modules.documents.persistence import DocumentRow
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.processing.persistence import ProcessingRunRow
from tests.integration.test_document_capture_postgresql import (
    CAPTURED_AT,
    PDF_CONTENT,
    create_workspace,
    make_capture_input,
    make_capture_service,
)

NOW = CAPTURED_AT + timedelta(seconds=1)


def capture_events(
    engine: Engine, tmp_path: Path, *, count: int = 1
) -> tuple[WorkspaceContext, tuple[OutboxEvent, ...]]:
    context = create_workspace(engine)
    service = make_capture_service(engine, tmp_path / "objects")
    events = []
    for _ in range(count):
        result = service.capture_pdf(context, make_capture_input(), BytesIO(PDF_CONTENT))
        events.append(
            OutboxEvent.for_processing(
                workspace_id=context.workspace_id,
                processing_run_id=result.processing_run_id,
                created_at=CAPTURED_AT,
            )
        )
    return context, tuple(events)


def read_event(engine: Engine, context: WorkspaceContext, event: OutboxEvent) -> OutboxEvent:
    with create_session_factory(engine)() as session:
        result = SqlAlchemyOutboxRepository(session).get_by_id(context, event.event_id)
        assert result is not None
        return result


def claim_event(engine: Engine, context: WorkspaceContext, *, now: datetime = NOW) -> OutboxEvent:
    factory = create_outbox_delivery_unit_of_work_factory(create_session_factory(engine))
    with factory() as work:
        claim = work.delivery.claim_next_due(context, now=now)
    assert claim is not None
    assert claim.state is OutboxState.CLAIMED
    assert claim.lease_id is not None
    return claim


def test_competing_claims_skip_locked_rows_and_have_disjoint_targets(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, events = capture_events(engine, tmp_path, count=2)
    factory = create_outbox_delivery_unit_of_work_factory(create_session_factory(engine))
    barrier = Barrier(2)

    def claim_while_other_transaction_is_open() -> OutboxEvent:
        with factory() as work:
            result = work.delivery.claim_next_due(context, now=NOW)
            barrier.wait(timeout=10)
            assert result is not None
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim_while_other_transaction_is_open) for _ in range(2)]
        claims = [future.result(timeout=15) for future in futures]
    assert {claim.event_id for claim in claims} == {event.event_id for event in events}
    assert all(claim.attempt_count == 1 for claim in claims)
    assert len({claim.lease_id for claim in claims}) == 2
    with factory() as work:
        assert work.delivery.claim_next_due(context, now=NOW) is None


def test_claim_rolls_back_with_its_transaction_and_no_foreign_work_is_exposed(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, (event,) = capture_events(engine, tmp_path)
    other = create_workspace(engine)
    factory = create_outbox_delivery_unit_of_work_factory(create_session_factory(engine))
    with pytest.raises(RuntimeError, match="synthetic rollback"), factory() as work:
        assert work.delivery.claim_next_due(context, now=NOW) is not None
        raise RuntimeError("synthetic rollback")
    assert read_event(engine, context, event) == event
    with factory() as work:
        assert work.delivery.claim_next_due(other, now=NOW) is None
    claim = claim_event(engine, context)
    assert claim.lease_id is not None
    with factory() as work:
        assert not work.delivery.record_published(
            other, event_id=claim.event_id, lease_id=claim.lease_id, now=NOW
        )
        assert not work.delivery.record_published(
            context, event_id=uuid4(), lease_id=claim.lease_id, now=NOW
        )
        assert not work.delivery.record_published(
            context, event_id=claim.event_id, lease_id=uuid4(), now=NOW
        )
    assert read_event(engine, context, event) == claim


def test_expiry_reclaims_with_a_new_token_and_rejects_both_stale_receipt_types(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, (event,) = capture_events(engine, tmp_path)
    factory = create_outbox_delivery_unit_of_work_factory(create_session_factory(engine))
    first = claim_event(engine, context)
    assert first.lease_id is not None
    expired_at = NOW + timedelta(seconds=30)
    with factory() as work:
        assert not work.delivery.record_published(
            context, event_id=event.event_id, lease_id=first.lease_id, now=expired_at
        )
    second = claim_event(engine, context, now=expired_at)
    assert second.attempt_count == 2
    assert second.lease_id != first.lease_id
    assert second.lease_id is not None
    with factory() as work:
        assert not work.delivery.record_failure(
            context,
            event_id=event.event_id,
            lease_id=first.lease_id,
            now=expired_at,
            category=DeliveryErrorCategory.INVALID_JOB,
            retry_delay_seconds=0,
        )
        assert not work.delivery.record_published(
            context, event_id=event.event_id, lease_id=first.lease_id, now=expired_at
        )
        assert work.delivery.record_published(
            context, event_id=event.event_id, lease_id=second.lease_id, now=expired_at
        )
    published = read_event(engine, context, event)
    assert published.state is OutboxState.PUBLISHED
    assert published.attempt_count == 2
    assert published.published_at == expired_at
    with factory() as work:
        assert not work.delivery.record_published(
            context, event_id=event.event_id, lease_id=second.lease_id, now=expired_at
        )
        assert work.delivery.claim_next_due(context, now=expired_at + timedelta(days=1)) is None
    with create_session_factory(engine)() as session:
        run = session.get(ProcessingRunRow, event.target_id)
        assert run is not None
        assert run.state is ProcessingState.CAPTURED
        assert run.attempt_count == 0


@pytest.mark.parametrize("publisher_disappears", [False, True])
def test_exhaustion_stays_blocked_without_resetting_attempts(
    integration_database_engine: Engine, tmp_path: Path, publisher_disappears: bool
) -> None:
    engine = integration_database_engine
    context, (event,) = capture_events(engine, tmp_path)
    factory = create_outbox_delivery_unit_of_work_factory(create_session_factory(engine))
    for attempt in range(3):
        now = NOW + timedelta(seconds=attempt * 30)
        claim = claim_event(engine, context, now=now)
        assert claim.attempt_count == attempt + 1
        assert claim.lease_id is not None
        if not publisher_disappears:
            with factory() as work:
                assert work.delivery.record_failure(
                    context,
                    event_id=event.event_id,
                    lease_id=claim.lease_id,
                    now=now,
                    category=DeliveryErrorCategory.DEPENDENCY_UNAVAILABLE,
                    retry_delay_seconds=30,
                )
                assert work.delivery.claim_next_due(context, now=now) is None
    with factory() as work:
        result = work.delivery.claim_next_due(context, now=NOW + timedelta(seconds=90))
        if publisher_disappears:
            assert result is not None and result.state is OutboxState.BLOCKED
        else:
            assert result is None
    blocked = read_event(engine, context, event)
    assert blocked.state is OutboxState.BLOCKED
    assert blocked.last_error_category is DeliveryErrorCategory.ATTEMPTS_EXHAUSTED
    assert blocked.attempt_count == 3
    assert blocked.generation == 0


@pytest.mark.parametrize("already_processing", [False, True])
def test_ineligible_or_already_started_targets_are_cancelled_without_consuming_a_claim(
    integration_database_engine: Engine, tmp_path: Path, already_processing: bool
) -> None:
    engine = integration_database_engine
    context, (event,) = capture_events(engine, tmp_path)
    sessions = create_session_factory(engine)
    with transactional_session(sessions) as session:
        if already_processing:
            session.execute(
                update(ProcessingRunRow)
                .where(ProcessingRunRow.id == event.target_id)
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
                .where(DocumentRow.workspace_id == context.workspace_id)
                .values(lifecycle_state=DocumentLifecycleState.PURGE_PENDING)
            )
    with create_outbox_delivery_unit_of_work_factory(sessions)() as work:
        result = work.delivery.claim_next_due(context, now=NOW)
        assert result is not None and result.state is OutboxState.CANCELLED
        assert result.attempt_count == 0
    with sessions() as session:
        run = session.get(ProcessingRunRow, event.target_id)
        assert run is not None
        assert run.attempt_count == (1 if already_processing else 0)


@pytest.mark.parametrize("delete_before_receipt", [False, True])
def test_receipt_allows_worker_start_but_never_ignores_pending_deletion(
    integration_database_engine: Engine, tmp_path: Path, delete_before_receipt: bool
) -> None:
    engine = integration_database_engine
    context, (event,) = capture_events(engine, tmp_path)
    claim = claim_event(engine, context)
    assert claim.lease_id is not None
    sessions = create_session_factory(engine)
    with transactional_session(sessions) as session:
        session.execute(
            update(ProcessingRunRow)
            .where(ProcessingRunRow.id == event.target_id)
            .values(
                state=ProcessingState.PARSING,
                attempt_count=1,
                started_at=NOW,
                lease_id=uuid4(),
                lease_expires_at=NOW + timedelta(seconds=360),
            )
        )
        if delete_before_receipt:
            session.execute(
                update(DocumentRow)
                .where(DocumentRow.workspace_id == context.workspace_id)
                .values(lifecycle_state=DocumentLifecycleState.PURGE_PENDING)
            )
    with create_outbox_delivery_unit_of_work_factory(sessions)() as work:
        accepted = work.delivery.record_published(
            context, event_id=event.event_id, lease_id=claim.lease_id, now=NOW
        )
    assert accepted is not delete_before_receipt
    expected = OutboxState.CANCELLED if delete_before_receipt else OutboxState.PUBLISHED
    assert read_event(engine, context, event).state is expected


def test_purge_cancellation_wins_over_a_late_receipt(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, (event,) = capture_events(engine, tmp_path)
    claim = claim_event(engine, context)
    assert claim.lease_id is not None
    sessions = create_session_factory(engine)
    with transactional_session(sessions) as session:
        SqlAlchemyOutboxRepository(session).cancel_processing_events(context, (event.target_id,))
    with create_outbox_delivery_unit_of_work_factory(sessions)() as work:
        assert not work.delivery.record_published(
            context, event_id=event.event_id, lease_id=claim.lease_id, now=NOW
        )
    with sessions() as session:
        row = session.scalar(
            select(OutboxEventRow).where(OutboxEventRow.event_id == event.event_id)
        )
        assert row is not None
        assert row.state is OutboxState.CANCELLED
        assert row.processing_run_id is None
        assert row.published_at is None


def test_empty_claim_still_rejects_naive_clock(integration_database_engine: Engine) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    with (
        pytest.raises(OutboxTransitionError),
        transactional_session(create_session_factory(engine)) as session,
    ):
        SqlAlchemyOutboxDeliveryRepository(session).claim_next_due(
            context, now=datetime(2026, 10, 19)
        )
