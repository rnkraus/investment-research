"""Shared work budgets across real canonical recovery and recording-only publication."""

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select

from investment_research.infrastructure.database.outbox_unit_of_work import (
    create_outbox_delivery_unit_of_work_factory,
)
from investment_research.infrastructure.database.session import create_session_factory
from investment_research.modules.delivery.application import DrainOutboxService
from investment_research.modules.delivery.budget import OutboxSweepBudget
from investment_research.modules.delivery.recovery import (
    RecoverExpiredOutboxService,
    RecoverUnstartedOutboxService,
)
from investment_research.modules.processing.contracts import ProcessingJobV1
from investment_research.modules.processing.persistence import ProcessingRunRow
from tests.integration.test_outbox_delivery_postgresql import NOW, capture_events, read_event
from tests.integration.test_outbox_recovery_postgresql import RECOVER_AT, publish_event


def test_one_budget_bounds_mixed_recovery_and_delivery_across_real_transactions(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context, _ = capture_events(engine, tmp_path, count=20)
    published = tuple(publish_event(engine, context, first=True) for _ in range(8))
    sessions = create_session_factory(engine)
    with sessions.begin() as session:
        for event in published[:4]:
            row = session.get(ProcessingRunRow, event.target_id)
            assert row is not None
            row.apply_state(
                row.to_domain().claim_for_processing(
                    lease_id=uuid4(),
                    claimed_at=NOW,
                    lease_expires_at=NOW + timedelta(seconds=360),
                    maximum_attempts=3,
                )
            )
    with sessions() as session:
        before = {
            row.id: row.to_domain()
            for row in session.scalars(
                select(ProcessingRunRow).where(
                    ProcessingRunRow.workspace_id == context.workspace_id
                )
            )
        }
    factory = create_outbox_delivery_unit_of_work_factory(sessions, maximum_processing_attempts=3)
    unstarted = RecoverUnstartedOutboxService(factory, clock=lambda: RECOVER_AT)
    expired = RecoverExpiredOutboxService(factory, maximum_attempts=3, clock=lambda: RECOVER_AT)
    jobs: list[ProcessingJobV1] = []

    class Publisher:
        def is_ready(self, *, timeout_seconds: float) -> bool:
            assert 0 < timeout_seconds <= 2
            return True

        def publish(self, job: ProcessingJobV1, *, timeout_seconds: float) -> None:
            assert 0 < timeout_seconds <= 2
            jobs.append(job)

    delivery = DrainOutboxService(factory, Publisher(), clock=lambda: RECOVER_AT)
    # Fixed monotonic time makes this a deterministic work-bound test, not a latency test.
    budget = OutboxSweepBudget(lambda: 0.0)
    with factory() as work:
        assert len(work.delivery.list_unstarted_candidates(context, now=RECOVER_AT, limit=2)) == 2
        assert (
            len(
                work.delivery.list_expired_candidates(
                    context, now=RECOVER_AT, maximum_attempts=3, limit=1
                )
            )
            == 1
        )
    first = unstarted.recover(context, budget=budget, max_targets=4)
    second = expired.recover(context, budget=budget, max_targets=4)
    sent = delivery.drain(context, budget=budget)
    assert (first.prepared, second.prepared, sent.published) == (4, 4, 12)
    assert budget.used_targets == 20 and budget.remaining_targets == 0
    assert delivery.drain(context, budget=budget).scanned == 0
    assert unstarted.recover(context, budget=budget).scanned == 0
    assert expired.recover(context, budget=budget).scanned == 0
    # Deferred prepared intent survives for a distinct invocation with a new allowance.
    following = OutboxSweepBudget(lambda: 0.0)
    assert delivery.drain(context, budget=following).published == 8
    assert following.used_targets == 8
    assert len(jobs) == len({job.processing_run_id for job in jobs}) == 20
    for event in published:
        assert read_event(engine, context, event) == event
    with sessions() as session:
        after = {
            row.id: row.to_domain()
            for row in session.scalars(
                select(ProcessingRunRow).where(
                    ProcessingRunRow.workspace_id == context.workspace_id
                )
            )
        }
    assert before == after


@pytest.mark.parametrize("limit", [0, -1, 21])
def test_database_candidate_limits_cannot_expand_the_accepted_bound(
    integration_database_engine: Engine, tmp_path: Path, limit: int
) -> None:
    engine = integration_database_engine
    context, _ = capture_events(engine, tmp_path)
    factory = create_outbox_delivery_unit_of_work_factory(create_session_factory(engine))
    with factory() as work:
        with pytest.raises(ValueError, match="between one and twenty"):
            work.delivery.list_unstarted_candidates(context, now=RECOVER_AT, limit=limit)
        with pytest.raises(ValueError, match="between one and twenty"):
            work.delivery.list_expired_candidates(
                context, now=RECOVER_AT, maximum_attempts=3, limit=limit
            )
