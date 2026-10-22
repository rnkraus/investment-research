"""Exhaustion retains receipts and is counted separately from prepared publication."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from investment_research.modules.delivery.budget import OutboxSweepBudget
from investment_research.modules.delivery.domain import (
    DeliveryErrorCategory,
    OutboxEvent,
    OutboxState,
    OutboxTransitionError,
)
from investment_research.modules.delivery.recovery import RecoveryStopReason
from tests.unit.test_outbox_contracts import NOW, make_event
from tests.unit.test_outbox_expired_recovery_scan import ExpiredHarness
from tests.unit.test_outbox_recovery_scan import Harness
from tests.unit.test_outbox_redelivery import published_event

MATURE = NOW + timedelta(seconds=420)


def test_blocked_recovery_preserves_the_original_acknowledgement_and_counters() -> None:
    published = published_event()
    blocked = published.block_exhausted_recovery(now=MATURE)
    assert blocked.state is OutboxState.BLOCKED
    assert blocked.last_error_category is DeliveryErrorCategory.ATTEMPTS_EXHAUSTED
    assert blocked.model_dump() == published.model_dump() | {
        "state": OutboxState.BLOCKED,
        "last_error_category": DeliveryErrorCategory.ATTEMPTS_EXHAUSTED,
    }
    assert published.state is OutboxState.PUBLISHED
    assert blocked.cancel().published_at == published.published_at
    with pytest.raises(OutboxTransitionError):
        blocked.redeliver_unstarted(now=MATURE)


@pytest.mark.parametrize("elapsed", [0, 419])
def test_exhaustion_cannot_bypass_publication_grace(elapsed: int) -> None:
    with pytest.raises(OutboxTransitionError):
        published_event().block_exhausted_recovery(now=NOW + timedelta(seconds=elapsed))


@pytest.mark.parametrize("state", ["pending", "cancelled", "blocked"])
def test_closed_or_unpublished_work_cannot_be_closed_as_published_exhaustion(state: str) -> None:
    event = {
        "pending": make_event(),
        "cancelled": published_event().cancel(),
        "blocked": published_event().block_exhausted_recovery(now=MATURE),
    }[state]
    with pytest.raises(OutboxTransitionError):
        event.block_exhausted_recovery(now=MATURE)


@pytest.mark.parametrize(
    "changes", [{"attempt_count": 0}, {"last_error_category": DeliveryErrorCategory.INVALID_JOB}]
)
def test_blocked_receipts_require_an_exhausted_acknowledged_attempt(
    changes: dict[str, object],
) -> None:
    blocked = published_event().block_exhausted_recovery(now=MATURE)
    with pytest.raises(ValidationError):
        OutboxEvent.model_validate(blocked.model_dump() | changes)


@pytest.mark.parametrize("expired", [False, True])
def test_prepared_skipped_and_blocked_results_are_counted_separately(expired: bool) -> None:
    harness = ExpiredHarness(3) if expired else Harness(3)
    service = harness.expired_service if isinstance(harness, ExpiredHarness) else harness.service
    recover = (
        harness.repository.recover_expired if expired else harness.repository.recover_unstarted
    )
    recover.side_effect = [
        published_event().block_exhausted_recovery(now=MATURE),
        None,
        make_event(),
    ]
    budget = OutboxSweepBudget(lambda: harness.elapsed)
    result = service.recover(harness.context, budget=budget)
    assert (result.scanned, result.prepared, result.blocked) == (3, 1, 1)
    assert budget.used_targets == 3
    harness.repository.claim_next_due.assert_not_called()


def test_blocking_stops_at_shared_limit_and_keeps_counts_after_deadline() -> None:
    harness = Harness(25)
    blocked = published_event().block_exhausted_recovery(now=MATURE)
    harness.repository.recover_unstarted.side_effect = None
    harness.repository.recover_unstarted.return_value = blocked
    result = harness.service.recover(harness.context)
    assert (result.scanned, result.prepared, result.blocked) == (20, 0, 20)
    assert result.stop_reason is RecoveryStopReason.TARGET_LIMIT

    def slow_block(*args: object, **kwargs: object) -> OutboxEvent:
        harness.elapsed = 30
        return blocked

    harness.elapsed = 0
    harness.repository.recover_unstarted.side_effect = slow_block
    result = harness.service.recover(harness.context)
    assert result.stop_reason is RecoveryStopReason.DEADLINE
    assert (result.scanned, result.prepared, result.blocked) == (1, 0, 1)
