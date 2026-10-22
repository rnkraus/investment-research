"""Recovery orchestration uses bounded snapshots and separate transactions, not a broker."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import Mock, call
from uuid import UUID, uuid4

import pytest

from investment_research.modules.delivery.domain import OutboxEvent
from investment_research.modules.delivery.ports import (
    OutboxDeliveryRepository,
    OutboxDeliveryUnitOfWork,
)
from investment_research.modules.delivery.recovery import (
    RecoverUnstartedOutboxService,
    RecoveryStopReason,
)
from investment_research.modules.identity.domain import WorkspaceContext
from tests.unit.test_outbox_contracts import NOW
from tests.unit.test_outbox_drain import Repositories


class Harness:
    def __init__(self, count: int = 1) -> None:
        self.context = WorkspaceContext(user_id=uuid4(), workspace_id=uuid4())
        self.candidates = tuple(uuid4() for _ in range(count))
        self.elapsed = 0.0
        self.transactions = 0
        self.active_transactions = 0
        self.repository = Mock(spec=OutboxDeliveryRepository)
        self.repository.list_unstarted_candidates.return_value = self.candidates
        self.repository.recover_unstarted.side_effect = self.recover
        self.service = RecoverUnstartedOutboxService(
            unit_of_work_factory=self.transaction,
            clock=lambda: NOW + timedelta(seconds=self.elapsed),
            monotonic_clock=lambda: self.elapsed,
        )

    @contextmanager
    def transaction(self) -> Iterator[OutboxDeliveryUnitOfWork]:
        assert self.active_transactions == 0
        self.active_transactions += 1
        self.transactions += 1
        try:
            yield Repositories(self.repository)
        finally:
            self.active_transactions -= 1

    def recover(self, context: WorkspaceContext, *, event_id: UUID, now: object) -> OutboxEvent:
        assert self.active_transactions == 1
        assert context == self.context and event_id in self.candidates
        return OutboxEvent.for_processing(
            workspace_id=context.workspace_id, processing_run_id=uuid4(), created_at=NOW
        )


def test_snapshot_and_each_target_have_separate_transactions() -> None:
    harness = Harness(3)
    result = harness.service.recover(harness.context)
    assert (result.scanned, result.prepared) == (3, 3)
    assert result.stop_reason is RecoveryStopReason.SNAPSHOT_EXHAUSTED
    assert harness.transactions == 4 and harness.active_transactions == 0
    harness.repository.list_unstarted_candidates.assert_called_once_with(
        harness.context, now=NOW, limit=20
    )
    assert harness.repository.recover_unstarted.call_args_list == [
        call(harness.context, event_id=identifier, now=NOW) for identifier in harness.candidates
    ]
    harness.repository.claim_next_due.assert_not_called()
    harness.repository.record_published.assert_not_called()


def test_empty_snapshot_has_no_target_transaction() -> None:
    harness = Harness(0)
    result = harness.service.recover(harness.context)
    assert (result.scanned, result.prepared) == (0, 0)
    assert result.stop_reason is RecoveryStopReason.SNAPSHOT_EXHAUSTED
    assert harness.transactions == 1
    harness.repository.recover_unstarted.assert_not_called()


def test_skipped_targets_count_toward_the_twenty_target_bound() -> None:
    harness = Harness(25)
    harness.repository.recover_unstarted.side_effect = None
    harness.repository.recover_unstarted.return_value = None
    result = harness.service.recover(harness.context)
    assert (result.scanned, result.prepared) == (20, 0)
    assert result.stop_reason is RecoveryStopReason.TARGET_LIMIT
    assert harness.transactions == 21


def test_slow_snapshot_cannot_start_target_recovery() -> None:
    harness = Harness()

    def snapshot(*args: object, **kwargs: object) -> tuple[UUID, ...]:
        harness.elapsed = 30
        return harness.candidates

    harness.repository.list_unstarted_candidates.side_effect = snapshot
    result = harness.service.recover(harness.context)
    assert result.stop_reason is RecoveryStopReason.DEADLINE
    assert result.scanned == result.prepared == 0
    harness.repository.recover_unstarted.assert_not_called()


def test_no_further_recovery_starts_after_deadline_and_time_is_fresh() -> None:
    harness = Harness(3)

    def recover(*args: object, **kwargs: object) -> None:
        harness.elapsed += 15

    harness.repository.recover_unstarted.side_effect = recover
    result = harness.service.recover(harness.context)
    assert (result.scanned, result.prepared) == (2, 0)
    assert result.stop_reason is RecoveryStopReason.DEADLINE
    assert [item.kwargs["now"] for item in harness.repository.recover_unstarted.call_args_list] == [
        NOW,
        NOW + timedelta(seconds=15),
    ]


@pytest.mark.parametrize("operation", ["list_unstarted_candidates", "recover_unstarted"])
def test_database_failures_are_not_disguised_as_success(operation: str) -> None:
    harness = Harness()
    getattr(harness.repository, operation).side_effect = RuntimeError("synthetic database failure")
    with pytest.raises(RuntimeError, match="synthetic database failure"):
        harness.service.recover(harness.context)
    assert harness.active_transactions == 0
