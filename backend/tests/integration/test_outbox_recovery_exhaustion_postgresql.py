"""Exhaustion closure is scoped, fenced, rollback-safe, and never a broker operation."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select

from investment_research.infrastructure.database.outbox_unit_of_work import (
    create_outbox_delivery_unit_of_work_factory,
)
from investment_research.infrastructure.database.session import create_session_factory
from investment_research.modules.delivery.domain import DeliveryErrorCategory, OutboxState
from investment_research.modules.delivery.persistence import OutboxEventRow
from investment_research.modules.delivery.repair import RepairOutboxService
from investment_research.modules.documents.persistence import DocumentRow
from investment_research.modules.processing.persistence import ProcessingRunRow
from tests.integration.test_document_capture_postgresql import create_workspace
from tests.integration.test_outbox_delivery_postgresql import read_event
from tests.integration.test_outbox_expired_recovery_postgresql import expired_target
from tests.integration.test_outbox_recovery_postgresql import RECOVER_AT


@pytest.mark.parametrize("lock_target", ["document", "run", "event"])
def test_exhaustion_cannot_bypass_locks_or_workspace_scope(
    integration_database_engine: Engine, tmp_path: Path, lock_target: str
) -> None:
    engine = integration_database_engine
    context, original, run = expired_target(engine, tmp_path)
    assert run.lease_id is not None
    sessions = create_session_factory(engine)
    with sessions.begin() as session:
        row = session.get(ProcessingRunRow, run.id)
        assert row is not None
        row.attempt_count = 3
    factory = create_outbox_delivery_unit_of_work_factory(sessions)
    other = create_workspace(engine)
    with factory() as work:
        for scope, identifier, token in (
            (other, original.event_id, run.lease_id),
            (context, uuid4(), run.lease_id),
            (context, original.event_id, uuid4()),
        ):
            assert (
                work.delivery.recover_expired(
                    scope,
                    event_id=identifier,
                    execution_lease_id=token,
                    now=RECOVER_AT,
                    maximum_attempts=3,
                )
                is None
            )
    with sessions.begin() as locked:
        statement = {
            "document": select(DocumentRow.id).where(
                DocumentRow.workspace_id == context.workspace_id
            ),
            "run": select(ProcessingRunRow.id).where(ProcessingRunRow.id == run.id),
            "event": select(OutboxEventRow.event_id).where(
                OutboxEventRow.event_id == original.event_id
            ),
        }[lock_target]
        locked.execute(statement.with_for_update()).one()
        with factory() as work:
            assert (
                work.delivery.recover_expired(
                    context,
                    event_id=original.event_id,
                    execution_lease_id=run.lease_id,
                    now=RECOVER_AT,
                    maximum_attempts=3,
                )
                is None
            )
    assert read_event(engine, context, original) == original


def test_exhaustion_preserves_live_fence_even_with_no_processing_attempts_left(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, original, run = expired_target(engine, tmp_path)
    assert run.lease_id is not None
    sessions = create_session_factory(engine)
    with sessions.begin() as session:
        row = session.get(ProcessingRunRow, run.id)
        assert row is not None
        row.attempt_count = 3
        row.lease_expires_at = RECOVER_AT + timedelta(seconds=1)
    factory = create_outbox_delivery_unit_of_work_factory(sessions)
    with factory() as work:
        assert (
            work.delivery.list_expired_candidates(context, now=RECOVER_AT, maximum_attempts=3) == ()
        )
        assert (
            work.delivery.recover_expired(
                context,
                event_id=original.event_id,
                execution_lease_id=run.lease_id,
                now=RECOVER_AT,
                maximum_attempts=3,
            )
            is None
        )
    assert read_event(engine, context, original) == original


def test_coordinator_closes_exhaustion_without_publishing_and_repeat_is_empty(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, original, run = expired_target(engine, tmp_path)
    sessions = create_session_factory(engine)
    with sessions.begin() as session:
        row = session.get(ProcessingRunRow, run.id)
        assert row is not None
        row.attempt_count = 3
        before = row.to_domain()
    factory = create_outbox_delivery_unit_of_work_factory(sessions, maximum_processing_attempts=3)
    publisher = Mock()
    publisher.is_ready.return_value = True
    service = RepairOutboxService(factory, publisher, 3, clock=lambda: RECOVER_AT)
    result = service.repair(context)
    assert result.expired.blocked == result.used_targets == 1
    assert result.expired.prepared == result.delivery.published == 0
    assert service.repair(context).used_targets == 0
    publisher.publish.assert_not_called()
    stored = read_event(engine, context, original)
    assert stored.state is OutboxState.BLOCKED
    assert stored.last_error_category is DeliveryErrorCategory.ATTEMPTS_EXHAUSTED
    assert stored.published_at == original.published_at
    with sessions() as session:
        row = session.get(ProcessingRunRow, run.id)
        assert row is not None and row.to_domain() == before


def test_exhaustion_rollback_and_competing_closures_retain_one_receipt(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, original, run = expired_target(engine, tmp_path)
    assert run.lease_id is not None
    token = run.lease_id
    sessions = create_session_factory(engine)
    with sessions.begin() as session:
        row = session.get(ProcessingRunRow, run.id)
        assert row is not None
        row.attempt_count = 3
    factory = create_outbox_delivery_unit_of_work_factory(sessions)
    with pytest.raises(RuntimeError, match="synthetic rollback"), factory() as work:
        result = work.delivery.recover_expired(
            context,
            event_id=original.event_id,
            execution_lease_id=token,
            now=RECOVER_AT,
            maximum_attempts=3,
        )
        assert result is not None and result.state is OutboxState.BLOCKED
        raise RuntimeError("synthetic rollback")
    assert read_event(engine, context, original) == original
    barrier = Barrier(2)

    def close() -> bool:
        with factory() as work:
            result = work.delivery.recover_expired(
                context,
                event_id=original.event_id,
                execution_lease_id=token,
                now=RECOVER_AT,
                maximum_attempts=3,
            )
            barrier.wait(timeout=10)
            return result is not None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(close) for _ in range(2)]
        assert sum(result.result(timeout=15) for result in results) == 1
    stored = read_event(engine, context, original)
    assert stored.state is OutboxState.BLOCKED and stored.published_at == original.published_at
