"""Explicitly enabled recovery dispatch, without Redis, providers, or normal archive data."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select, update

from investment_research.infrastructure.database.outbox_unit_of_work import (
    create_outbox_delivery_unit_of_work_factory,
)
from investment_research.infrastructure.database.session import create_session_factory
from investment_research.modules.delivery.application import DrainOutboxService
from investment_research.modules.delivery.domain import (
    DeliveryErrorCategory,
    OutboxEvent,
    OutboxState,
)
from investment_research.modules.delivery.persistence import OutboxEventRow
from investment_research.modules.documents.domain import DocumentLifecycleState
from investment_research.modules.documents.persistence import DocumentRow
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.contracts import ProcessingJobV1
from investment_research.modules.processing.domain import ProcessingRun, ProcessingState
from investment_research.modules.processing.persistence import ProcessingRunRow
from tests.integration.test_document_capture_postgresql import (
    PDF_CONTENT,
    create_workspace,
    make_capture_input,
    make_capture_service,
)
from tests.integration.test_outbox_delivery_postgresql import read_event
from tests.integration.test_outbox_expired_recovery_postgresql import expired_target
from tests.integration.test_outbox_recovery_postgresql import RECOVER_AT


def prepared_target(
    engine: Engine, tmp_path: Path, state: ProcessingState = ProcessingState.PARSING
) -> tuple[WorkspaceContext, OutboxEvent, OutboxEvent, ProcessingRun]:
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
        assert successor is not None
    return context, original, successor, run


@pytest.mark.parametrize(
    "state", [ProcessingState.PARSING, ProcessingState.LEXICAL_READY, ProcessingState.INDEXING]
)
def test_recovery_dispatch_commits_before_publish_and_accepts_a_new_worker_fence(
    integration_database_engine: Engine, tmp_path: Path, state: ProcessingState
) -> None:
    engine = integration_database_engine
    context, original, successor, run = prepared_target(engine, tmp_path, state)
    sessions = create_session_factory(engine)
    factory = create_outbox_delivery_unit_of_work_factory(sessions, maximum_processing_attempts=3)
    new_lease = uuid4()
    jobs: list[ProcessingJobV1] = []

    class Publisher:
        def is_ready(self, *, timeout_seconds: float) -> bool:
            assert 0 < timeout_seconds <= 2
            return True

        def publish(self, job: ProcessingJobV1, *, timeout_seconds: float) -> None:
            assert 0 < timeout_seconds <= 2
            assert job.workspace_id == context.workspace_id and job.processing_run_id == run.id
            assert read_event(engine, context, successor).state is OutboxState.CLAIMED
            # A fake consumer must be able to lock the run on another connection immediately.
            with sessions.begin() as session:
                row = session.scalars(
                    select(ProcessingRunRow)
                    .where(ProcessingRunRow.id == run.id)
                    .with_for_update(nowait=True)
                ).one()
                assert row.to_domain() == run
                claim = (
                    run.claim_for_processing
                    if state is ProcessingState.PARSING
                    else run.claim_for_indexing
                )
                row.apply_state(
                    claim(
                        lease_id=new_lease,
                        claimed_at=RECOVER_AT,
                        lease_expires_at=RECOVER_AT + timedelta(seconds=360),
                        maximum_attempts=3,
                    )
                )
            jobs.append(job)

    result = DrainOutboxService(factory, Publisher(), clock=lambda: RECOVER_AT).drain(context)
    assert result.published == result.claimed == 1 and result.rejected_receipts == 0
    assert len(jobs) == 1
    assert read_event(engine, context, original) == original
    assert read_event(engine, context, successor).state is OutboxState.PUBLISHED
    with sessions() as session:
        stored = session.get(ProcessingRunRow, run.id)
        assert stored is not None and stored.attempt_count == 2 and stored.lease_id == new_lease


def test_default_dispatch_preserves_recovery_until_profile_is_explicit(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, _, successor, _ = prepared_target(engine, tmp_path)
    sessions = create_session_factory(engine)
    with create_outbox_delivery_unit_of_work_factory(sessions)() as work:
        assert work.delivery.claim_next_due(context, now=RECOVER_AT) is None
    assert read_event(engine, context, successor) == successor
    other = create_workspace(engine)
    with create_outbox_delivery_unit_of_work_factory(
        sessions, maximum_processing_attempts=3
    )() as work:
        assert work.delivery.claim_next_due(other, now=RECOVER_AT) is None
        result = work.delivery.claim_next_due(context, now=RECOVER_AT)
        assert result is not None and result.state is OutboxState.CLAIMED
        assert result.attempt_count == 1


@pytest.mark.parametrize("change", ["live", "exhausted", "terminal", "excluded"])
def test_dispatch_requalifies_changed_execution_without_consuming_an_attempt(
    integration_database_engine: Engine, tmp_path: Path, change: str
) -> None:
    engine = integration_database_engine
    context, _, successor, run = prepared_target(engine, tmp_path)
    assert run.lease_id is not None
    sessions = create_session_factory(engine)
    with sessions.begin() as session:
        row = session.get(ProcessingRunRow, run.id)
        assert row is not None
        if change == "live":
            row.apply_state(
                run.claim_for_processing(
                    lease_id=uuid4(),
                    claimed_at=RECOVER_AT,
                    lease_expires_at=RECOVER_AT + timedelta(seconds=360),
                    maximum_attempts=3,
                )
            )
        elif change == "exhausted":
            row.attempt_count = 3
        elif change == "terminal":
            row.apply_state(run.mark_lexical_ready(lease_id=run.lease_id, completed_at=RECOVER_AT))
        else:
            session.execute(
                update(DocumentRow)
                .where(DocumentRow.workspace_id == context.workspace_id)
                .values(lifecycle_state=DocumentLifecycleState.PURGE_PENDING)
            )
    with create_outbox_delivery_unit_of_work_factory(
        sessions, maximum_processing_attempts=3
    )() as work:
        result = work.delivery.claim_next_due(context, now=RECOVER_AT)
    if change == "live":
        assert result is None and read_event(engine, context, successor) == successor
    else:
        assert result is not None and result.attempt_count == 0
        assert result.state is (
            OutboxState.BLOCKED if change == "exhausted" else OutboxState.CANCELLED
        )
        if change == "exhausted":
            assert result.last_error_category is DeliveryErrorCategory.ATTEMPTS_EXHAUSTED
    with sessions() as session:
        stored = session.get(ProcessingRunRow, run.id)
        assert stored is not None and stored.attempt_count == (
            3 if change == "exhausted" else 2 if change == "live" else 1
        )


@pytest.mark.parametrize("lock_target", ["document", "run", "event"])
def test_busy_recovery_targets_are_preserved_not_cancelled(
    integration_database_engine: Engine, tmp_path: Path, lock_target: str
) -> None:
    engine = integration_database_engine
    context, _, successor, run = prepared_target(engine, tmp_path)
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
                .where(OutboxEventRow.event_id == successor.event_id)
                .with_for_update()
            ).one()
        with create_outbox_delivery_unit_of_work_factory(
            sessions, maximum_processing_attempts=3
        )() as work:
            assert work.delivery.claim_next_due(context, now=RECOVER_AT) is None
    assert read_event(engine, context, successor) == successor


def test_live_recovery_does_not_hide_other_due_work(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, _, successor, run = prepared_target(engine, tmp_path)
    accepted = make_capture_service(engine, tmp_path / "another").capture_pdf(
        context, make_capture_input(), BytesIO(PDF_CONTENT)
    )
    sessions = create_session_factory(engine)
    with sessions.begin() as session:
        session.execute(
            update(ProcessingRunRow)
            .where(ProcessingRunRow.id == run.id)
            .values(
                lease_id=uuid4(),
                lease_expires_at=RECOVER_AT + timedelta(seconds=360),
                attempt_count=2,
            )
        )
        session.execute(
            update(OutboxEventRow)
            .where(OutboxEventRow.target_id == accepted.processing_run_id)
            .values(next_attempt_at=RECOVER_AT + timedelta(seconds=1))
        )
    with create_outbox_delivery_unit_of_work_factory(
        sessions, maximum_processing_attempts=3
    )() as work:
        result = work.delivery.claim_next_due(context, now=RECOVER_AT + timedelta(seconds=1))
        assert result is not None and result.target_id == accepted.processing_run_id
        assert result.state is OutboxState.CLAIMED
    assert read_event(engine, context, successor) == successor


def test_rollback_and_competing_dispatchers_retain_one_claim(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, _, successor, _ = prepared_target(engine, tmp_path)
    factory = create_outbox_delivery_unit_of_work_factory(
        create_session_factory(engine), maximum_processing_attempts=3
    )
    with pytest.raises(RuntimeError, match="synthetic rollback"), factory() as work:
        assert work.delivery.claim_next_due(context, now=RECOVER_AT) is not None
        raise RuntimeError("synthetic rollback")
    assert read_event(engine, context, successor) == successor
    barrier = Barrier(2)

    def claim() -> OutboxEvent | None:
        with factory() as work:
            result = work.delivery.claim_next_due(context, now=RECOVER_AT)
            barrier.wait(timeout=10)
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        assert sum(future.result(timeout=15) is not None for future in futures) == 1
    assert read_event(engine, context, successor).attempt_count == 1
