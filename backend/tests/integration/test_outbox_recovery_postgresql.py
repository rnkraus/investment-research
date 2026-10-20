"""Scoped published-message recovery, with no broker or user archive access."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, select, update

from investment_research.infrastructure.database.outbox_unit_of_work import (
    create_outbox_delivery_unit_of_work_factory,
)
from investment_research.infrastructure.database.session import create_session_factory
from investment_research.modules.delivery.domain import OutboxEvent, OutboxState
from investment_research.modules.delivery.persistence import OutboxEventRow
from investment_research.modules.documents.domain import DocumentLifecycleState
from investment_research.modules.documents.persistence import DocumentRow, DocumentVersionRow
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.processing.persistence import ProcessingRunRow
from tests.integration.test_document_capture_postgresql import create_workspace
from tests.integration.test_outbox_delivery_postgresql import (
    NOW,
    capture_events,
    claim_event,
    read_event,
)

RECOVER_AT = NOW + timedelta(seconds=420)


def publish_event(engine: Engine, context: WorkspaceContext, *, first: bool = False) -> OutboxEvent:
    now = NOW if first else RECOVER_AT
    claim = claim_event(engine, context, now=now)
    assert claim.lease_id is not None
    with create_outbox_delivery_unit_of_work_factory(create_session_factory(engine))() as work:
        assert work.delivery.record_published(
            context, event_id=claim.event_id, lease_id=claim.lease_id, now=now
        )
    return read_event(engine, context, claim)


def test_recovery_is_scoped_due_idempotent_and_preserves_existing_successors(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, _ = capture_events(engine, tmp_path)
    original = publish_event(engine, context, first=True)
    factory = create_outbox_delivery_unit_of_work_factory(create_session_factory(engine))
    other = create_workspace(engine)
    with factory() as work:
        for scope, identifier in ((other, original.event_id), (context, uuid4())):
            assert (
                work.delivery.recover_unstarted(scope, event_id=identifier, now=RECOVER_AT) is None
            )
        assert (
            work.delivery.recover_unstarted(
                context, event_id=original.event_id, now=RECOVER_AT - timedelta(seconds=1)
            )
            is None
        )
        next_event = work.delivery.recover_unstarted(
            context, event_id=original.event_id, now=RECOVER_AT
        )
        assert next_event is not None and next_event.generation == 1
        assert next_event.state is OutboxState.PENDING
    published_next = publish_event(engine, context)
    with factory() as work:
        assert (
            work.delivery.recover_unstarted(
                context, event_id=original.event_id, now=RECOVER_AT + timedelta(seconds=1)
            )
            == published_next
        )
        final = work.delivery.recover_unstarted(
            context, event_id=published_next.event_id, now=RECOVER_AT + timedelta(seconds=420)
        )
        assert final is not None and final.generation == 2
        assert (
            work.delivery.recover_unstarted(
                context, event_id=original.event_id, now=RECOVER_AT + timedelta(seconds=421)
            )
            is None
        )
    with factory() as work:
        final_claim = work.delivery.claim_next_due(context, now=RECOVER_AT + timedelta(seconds=420))
        assert final_claim is not None and final_claim.lease_id is not None
        assert work.delivery.record_published(
            context,
            event_id=final_claim.event_id,
            lease_id=final_claim.lease_id,
            now=RECOVER_AT + timedelta(seconds=420),
        )
        assert (
            work.delivery.recover_unstarted(
                context, event_id=final_claim.event_id, now=RECOVER_AT + timedelta(seconds=840)
            )
            is None
        )
    assert read_event(engine, context, original) == original
    assert read_event(engine, context, published_next) == published_next
    with create_session_factory(engine)() as session:
        run = session.get(ProcessingRunRow, original.target_id)
        assert run is not None and run.attempt_count == 0


def test_competing_recovery_transactions_create_only_one_generation(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, _ = capture_events(engine, tmp_path)
    original = publish_event(engine, context, first=True)
    factory = create_outbox_delivery_unit_of_work_factory(create_session_factory(engine))
    barrier = Barrier(2)

    def recover() -> OutboxEvent | None:
        with factory() as work:
            result = work.delivery.recover_unstarted(
                context, event_id=original.event_id, now=RECOVER_AT
            )
            barrier.wait(timeout=10)
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(recover) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert sum(result is not None for result in results) == 1
    with create_session_factory(engine)() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEventRow)
                .where(OutboxEventRow.workspace_id == context.workspace_id)
            )
            == 2
        )


def test_recovery_is_part_of_the_callers_transaction(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, _ = capture_events(engine, tmp_path)
    original = publish_event(engine, context, first=True)
    factory = create_outbox_delivery_unit_of_work_factory(create_session_factory(engine))
    with pytest.raises(RuntimeError, match="synthetic rollback"), factory() as work:
        assert (
            work.delivery.recover_unstarted(context, event_id=original.event_id, now=RECOVER_AT)
            is not None
        )
        raise RuntimeError("synthetic rollback")
    assert read_event(engine, context, original) == original
    with create_session_factory(engine)() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEventRow)
                .where(OutboxEventRow.workspace_id == context.workspace_id)
            )
            == 1
        )


@pytest.mark.parametrize(
    "condition", ["started", "expired", "completed", "pending_deletion", "inactive"]
)
def test_canonical_ineligibility_never_creates_a_generation(
    integration_database_engine: Engine, tmp_path: Path, condition: str
) -> None:
    engine = integration_database_engine
    context, _ = capture_events(engine, tmp_path)
    original = publish_event(engine, context, first=True)
    sessions = create_session_factory(engine)
    with sessions.begin() as session:
        if condition in {"started", "expired", "completed"}:
            session.execute(
                update(ProcessingRunRow)
                .where(ProcessingRunRow.id == original.target_id)
                .values(
                    state=ProcessingState.LEXICAL_READY
                    if condition == "completed"
                    else ProcessingState.PARSING,
                    attempt_count=1,
                    started_at=NOW,
                    completed_at=NOW if condition == "completed" else None,
                    lease_id=None if condition == "completed" else uuid4(),
                    lease_expires_at=(
                        None
                        if condition == "completed"
                        else NOW + timedelta(seconds=1 if condition == "expired" else 600)
                    ),
                )
            )
        elif condition == "pending_deletion":
            session.execute(
                update(DocumentRow)
                .where(DocumentRow.workspace_id == context.workspace_id)
                .values(lifecycle_state=DocumentLifecycleState.PURGE_PENDING)
            )
        else:
            old_version = session.scalars(
                select(DocumentVersionRow).where(
                    DocumentVersionRow.workspace_id == context.workspace_id
                )
            ).one()
            replacement = old_version.to_domain().model_copy(
                update={"id": uuid4(), "version_number": 2}
            )
            session.add(DocumentVersionRow.from_domain(replacement))
            session.flush()
            session.execute(
                update(DocumentRow)
                .where(DocumentRow.workspace_id == context.workspace_id)
                .values(current_version_id=replacement.id)
            )
    with create_outbox_delivery_unit_of_work_factory(sessions)() as work:
        assert (
            work.delivery.recover_unstarted(context, event_id=original.event_id, now=RECOVER_AT)
            is None
        )
    assert read_event(engine, context, original) == original


@pytest.mark.parametrize("lock_target", ["document", "run", "latest"])
def test_locked_work_is_skipped_without_falling_back_to_an_older_generation(
    integration_database_engine: Engine, tmp_path: Path, lock_target: str
) -> None:
    engine = integration_database_engine
    context, _ = capture_events(engine, tmp_path)
    original = publish_event(engine, context, first=True)
    sessions = create_session_factory(engine)
    factory = create_outbox_delivery_unit_of_work_factory(sessions)
    with factory() as work:
        successor = work.delivery.recover_unstarted(
            context, event_id=original.event_id, now=RECOVER_AT
        )
        assert successor is not None
    with sessions.begin() as locked:
        if lock_target == "document":
            locked.scalars(
                select(DocumentRow)
                .where(DocumentRow.workspace_id == context.workspace_id)
                .with_for_update()
            ).one()
        elif lock_target == "run":
            locked.scalars(
                select(ProcessingRunRow)
                .where(ProcessingRunRow.id == original.target_id)
                .with_for_update()
            ).one()
        else:
            locked.scalars(
                select(OutboxEventRow)
                .where(OutboxEventRow.event_id == successor.event_id)
                .with_for_update()
            ).one()
        with factory() as work:
            assert (
                work.delivery.recover_unstarted(context, event_id=original.event_id, now=RECOVER_AT)
                is None
            )
    assert read_event(engine, context, original) == original
    assert read_event(engine, context, successor) == successor
