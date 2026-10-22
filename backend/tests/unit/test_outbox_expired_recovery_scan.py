"""Expired recovery scans retain observed fences and bounded independent transactions."""

from datetime import timedelta
from unittest.mock import call
from uuid import uuid4

import pytest

from investment_research.modules.delivery.domain import OutboxEvent
from investment_research.modules.delivery.ports import ExpiredOutboxCandidate
from investment_research.modules.delivery.recovery import (
    RecoverExpiredOutboxService,
    RecoveryStopReason,
)
from tests.unit.test_outbox_contracts import NOW
from tests.unit.test_outbox_recovery_scan import Harness


class ExpiredHarness(Harness):
    def __init__(self, count: int = 1) -> None:
        super().__init__(count)
        self.expired_candidates = tuple(
            ExpiredOutboxCandidate(identifier, uuid4()) for identifier in self.candidates
        )
        self.repository.list_expired_candidates.return_value = self.expired_candidates
        self.repository.recover_expired.side_effect = None
        self.repository.recover_expired.return_value = None
        self.expired_service = RecoverExpiredOutboxService(
            self.transaction,
            maximum_attempts=3,
            clock=lambda: NOW + timedelta(seconds=self.elapsed),
            monotonic_clock=lambda: self.elapsed,
        )


def test_observed_fences_and_profile_limits_are_forwarded_in_separate_transactions() -> None:
    harness = ExpiredHarness(3)
    harness.repository.recover_expired.return_value = OutboxEvent.for_processing(
        workspace_id=harness.context.workspace_id, processing_run_id=uuid4(), created_at=NOW
    )
    result = harness.expired_service.recover(harness.context)
    assert (result.scanned, result.prepared) == (3, 3)
    assert result.stop_reason is RecoveryStopReason.SNAPSHOT_EXHAUSTED
    assert harness.transactions == 4 and harness.active_transactions == 0
    harness.repository.list_expired_candidates.assert_called_once_with(
        harness.context, now=NOW, maximum_attempts=3, limit=20
    )
    assert harness.repository.recover_expired.call_args_list == [
        call(
            harness.context,
            event_id=candidate.event_id,
            execution_lease_id=candidate.execution_lease_id,
            now=NOW,
            maximum_attempts=3,
        )
        for candidate in harness.expired_candidates
    ]
    harness.repository.claim_next_due.assert_not_called()
    harness.repository.record_published.assert_not_called()
    harness.repository.recover_unstarted.assert_not_called()


def test_empty_expired_snapshot_has_no_target_transaction() -> None:
    harness = ExpiredHarness(0)
    result = harness.expired_service.recover(harness.context)
    assert result.scanned == result.prepared == 0
    assert result.stop_reason is RecoveryStopReason.SNAPSHOT_EXHAUSTED
    assert harness.transactions == 1


def test_changed_or_skipped_fences_still_consume_the_twenty_target_bound() -> None:
    harness = ExpiredHarness(25)
    result = harness.expired_service.recover(harness.context)
    assert (result.scanned, result.prepared) == (20, 0)
    assert result.stop_reason is RecoveryStopReason.TARGET_LIMIT
    assert harness.transactions == 21


def test_slow_expired_snapshot_cannot_start_recovery() -> None:
    harness = ExpiredHarness()

    def snapshot(*args: object, **kwargs: object) -> tuple[ExpiredOutboxCandidate, ...]:
        harness.elapsed = 30
        return harness.expired_candidates

    harness.repository.list_expired_candidates.side_effect = snapshot
    result = harness.expired_service.recover(harness.context)
    assert result.stop_reason is RecoveryStopReason.DEADLINE
    assert result.scanned == result.prepared == 0
    harness.repository.recover_expired.assert_not_called()


def test_recovery_refreshes_time_and_stops_at_the_deadline() -> None:
    harness = ExpiredHarness(3)

    def recover(*args: object, **kwargs: object) -> None:
        assert harness.active_transactions == 1
        harness.elapsed += 15

    harness.repository.recover_expired.side_effect = recover
    result = harness.expired_service.recover(harness.context)
    assert (result.scanned, result.prepared) == (2, 0)
    assert result.stop_reason is RecoveryStopReason.DEADLINE
    assert [item.kwargs["now"] for item in harness.repository.recover_expired.call_args_list] == [
        NOW,
        NOW + timedelta(seconds=15),
    ]


@pytest.mark.parametrize("operation", ["list_expired_candidates", "recover_expired"])
def test_database_errors_are_not_reported_as_success(operation: str) -> None:
    harness = ExpiredHarness()
    getattr(harness.repository, operation).side_effect = RuntimeError("synthetic database failure")
    with pytest.raises(RuntimeError, match="synthetic database failure"):
        harness.expired_service.recover(harness.context)
    assert harness.active_transactions == 0


@pytest.mark.parametrize("maximum_attempts", [0, -1])
def test_invalid_profile_limit_is_rejected_before_any_transaction(maximum_attempts: int) -> None:
    harness = ExpiredHarness()
    with pytest.raises(ValueError, match="maximum_attempts must be positive"):
        RecoverExpiredOutboxService(harness.transaction, maximum_attempts=maximum_attempts)
    assert harness.transactions == 0
