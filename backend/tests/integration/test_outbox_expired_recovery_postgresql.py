"""Prepare expired-execution redelivery without broker calls or execution mutations."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, select, update

from investment_research.infrastructure.database.outbox_delivery import (
    SqlAlchemyOutboxDeliveryRepository,
)
from investment_research.infrastructure.database.outbox_unit_of_work import (
    create_outbox_delivery_unit_of_work_factory,
)
from investment_research.infrastructure.database.session import create_session_factory
from investment_research.modules.delivery.domain import OutboxEvent, OutboxState
from investment_research.modules.delivery.persistence import OutboxEventRow
from investment_research.modules.documents.domain import DocumentLifecycleState
from investment_research.modules.documents.persistence import DocumentRow, DocumentVersionRow
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.application import M1_PROCESSING_PROFILE
from investment_research.modules.processing.domain import ProcessingRun, ProcessingState
from investment_research.modules.processing.persistence import ProcessingRunRow
from tests.integration.test_document_capture_postgresql import create_workspace
from tests.integration.test_outbox_delivery_postgresql import NOW, capture_events, read_event
from tests.integration.test_outbox_recovery_postgresql import RECOVER_AT, publish_event


def expired_target(
    engine: Engine, tmp_path: Path, state: ProcessingState = ProcessingState.PARSING
) -> tuple[WorkspaceContext, OutboxEvent, ProcessingRun]:
    context, _ = capture_events(engine, tmp_path)
    original = publish_event(engine, context, first=True)
    with create_session_factory(engine).begin() as session:
        row = session.get(ProcessingRunRow, original.target_id)
        assert row is not None
        if state is not ProcessingState.PARSING:
            # Arrange synthetic M1 metadata before execution; this is not a version-change API.
            row.embedding_config_version = M1_PROCESSING_PROFILE.embedding_config_version
            row.dense_index_config_version = M1_PROCESSING_PROFILE.dense_index_config_version
        execution_lease_id = uuid4()
        run = row.to_domain().claim_for_processing(
            lease_id=execution_lease_id,
            claimed_at=NOW,
            lease_expires_at=NOW + timedelta(seconds=360),
            maximum_attempts=3,
        )
        if state is not ProcessingState.PARSING:
            run = run.mark_lexical_ready_for_indexing(lease_id=execution_lease_id)
            if state is ProcessingState.INDEXING:
                run = run.begin_indexing(lease_id=execution_lease_id)
        row.apply_state(run)
    return context, original, run


@pytest.mark.parametrize(
    "state", [ProcessingState.PARSING, ProcessingState.LEXICAL_READY, ProcessingState.INDEXING]
)
def test_expired_recovery_preserves_execution_and_is_idempotent(
    integration_database_engine: Engine, tmp_path: Path, state: ProcessingState
) -> None:
    engine = integration_database_engine
    context, original, run = expired_target(engine, tmp_path, state)
    assert run.lease_id is not None
    factory = create_outbox_delivery_unit_of_work_factory(create_session_factory(engine))
    with factory() as work:
        successor = work.delivery.recover_expired(
            context,
            event_id=original.event_id,
            execution_lease_id=run.lease_id,
            now=RECOVER_AT,
            maximum_attempts=3,
        )
        assert successor is not None and successor.generation == 1
        assert successor.state is OutboxState.PENDING and successor.attempt_count == 0
    with factory() as work:
        assert (
            work.delivery.recover_expired(
                context,
                event_id=original.event_id,
                execution_lease_id=run.lease_id,
                now=RECOVER_AT + timedelta(seconds=1),
                maximum_attempts=3,
            )
            == successor
        )
    with create_session_factory(engine)() as session:
        stored = session.get(ProcessingRunRow, run.id)
        assert stored is not None and stored.to_domain() == run
    assert read_event(engine, context, original) == original


def test_expired_recovery_hides_foreign_unknown_and_changed_fences(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, original, run = expired_target(engine, tmp_path)
    assert run.lease_id is not None
    other = create_workspace(engine)
    factory = create_outbox_delivery_unit_of_work_factory(create_session_factory(engine))
    with factory() as work:
        for scope, event_id, lease_id in (
            (other, original.event_id, run.lease_id),
            (context, uuid4(), run.lease_id),
            (context, original.event_id, uuid4()),
        ):
            assert (
                work.delivery.recover_expired(
                    scope,
                    event_id=event_id,
                    execution_lease_id=lease_id,
                    now=RECOVER_AT,
                    maximum_attempts=3,
                )
                is None
            )


@pytest.mark.parametrize(
    "condition", ["live", "exhausted", "renewed", "terminal", "excluded", "inactive"]
)
def test_expired_recovery_rechecks_canonical_eligibility(
    integration_database_engine: Engine, tmp_path: Path, condition: str
) -> None:
    engine = integration_database_engine
    context, original, run = expired_target(engine, tmp_path)
    assert run.lease_id is not None
    sessions = create_session_factory(engine)
    with sessions.begin() as session:
        row = session.get(ProcessingRunRow, run.id)
        assert row is not None
        if condition == "live":
            row.lease_expires_at = RECOVER_AT + timedelta(seconds=1)
        elif condition == "exhausted":
            row.attempt_count = 3
        elif condition == "renewed":
            row.lease_id = uuid4()
            row.attempt_count = 2
        elif condition == "terminal":
            row.apply_state(
                run.mark_failed(
                    lease_id=run.lease_id,
                    completed_at=RECOVER_AT,
                    error_summary="Synthetic terminal failure",
                )
            )
        elif condition == "excluded":
            session.execute(
                update(DocumentRow)
                .where(DocumentRow.workspace_id == context.workspace_id)
                .values(lifecycle_state=DocumentLifecycleState.PURGE_PENDING)
            )
        else:
            old = session.get(DocumentVersionRow, run.document_version_id)
            assert old is not None
            replacement = old.to_domain().model_copy(update={"id": uuid4(), "version_number": 2})
            session.add(DocumentVersionRow.from_domain(replacement))
            session.flush()
            session.execute(
                update(DocumentRow)
                .where(DocumentRow.workspace_id == context.workspace_id)
                .values(current_version_id=replacement.id)
            )
    with create_outbox_delivery_unit_of_work_factory(sessions)() as work:
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


@pytest.mark.parametrize("lock_target", ["document", "run", "event"])
def test_expired_recovery_skips_locked_canonical_targets(
    integration_database_engine: Engine, tmp_path: Path, lock_target: str
) -> None:
    engine = integration_database_engine
    context, original, run = expired_target(engine, tmp_path)
    assert run.lease_id is not None
    sessions = create_session_factory(engine)
    with sessions.begin() as locked:
        if lock_target == "document":
            locked.scalars(
                select(DocumentRow)
                .where(DocumentRow.workspace_id == context.workspace_id)
                .with_for_update()
            ).one()
        elif lock_target == "run":
            locked.scalars(
                select(ProcessingRunRow).where(ProcessingRunRow.id == run.id).with_for_update()
            ).one()
        else:
            locked.scalars(
                select(OutboxEventRow)
                .where(OutboxEventRow.event_id == original.event_id)
                .with_for_update()
            ).one()
        with create_outbox_delivery_unit_of_work_factory(sessions)() as work:
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


def test_competing_expired_recovery_keeps_one_generation_and_rolls_back_atomically(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, original, run = expired_target(engine, tmp_path)
    lease_id = run.lease_id
    assert lease_id is not None
    factory = create_outbox_delivery_unit_of_work_factory(create_session_factory(engine))
    with pytest.raises(RuntimeError, match="synthetic rollback"), factory() as work:
        assert (
            work.delivery.recover_expired(
                context,
                event_id=original.event_id,
                execution_lease_id=lease_id,
                now=RECOVER_AT,
                maximum_attempts=3,
            )
            is not None
        )
        raise RuntimeError("synthetic rollback")
    with create_session_factory(engine)() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEventRow)
                .where(OutboxEventRow.workspace_id == context.workspace_id)
            )
            == 1
        )
    barrier = Barrier(2)

    def recover() -> OutboxEvent | None:
        with factory() as work:
            result = work.delivery.recover_expired(
                context,
                event_id=original.event_id,
                execution_lease_id=lease_id,
                now=RECOVER_AT,
                maximum_attempts=3,
            )
            barrier.wait(timeout=10)
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(recover) for _ in range(2)]
        assert sum(future.result(timeout=15) is not None for future in futures) == 1


def test_recovery_refreshes_a_stale_loaded_execution_snapshot(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, original, run = expired_target(engine, tmp_path)
    assert run.lease_id is not None
    sessions = create_session_factory(engine)
    replacement_lease = uuid4()
    with sessions.begin() as stale:
        loaded = stale.get(ProcessingRunRow, run.id)
        assert loaded is not None and loaded.lease_id == run.lease_id
        with sessions.begin() as changed:
            changed.execute(
                update(ProcessingRunRow)
                .where(ProcessingRunRow.id == run.id)
                .values(lease_id=replacement_lease, attempt_count=2)
            )
        assert (
            SqlAlchemyOutboxDeliveryRepository(stale).recover_expired(
                context,
                event_id=original.event_id,
                execution_lease_id=run.lease_id,
                now=RECOVER_AT,
                maximum_attempts=3,
            )
            is None
        )
        assert loaded.lease_id == replacement_lease
