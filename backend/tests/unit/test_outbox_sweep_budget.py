"""Combined preparation/publication phases cannot renew work or time allowances."""

from uuid import uuid4

import pytest

from investment_research.modules.delivery.application import DrainStopReason
from investment_research.modules.delivery.budget import OutboxSweepBudget
from investment_research.modules.delivery.domain import OutboxEvent
from investment_research.modules.delivery.ports import ExpiredOutboxCandidate
from investment_research.modules.delivery.recovery import (
    RecoverExpiredOutboxService,
    RecoverUnstartedOutboxService,
    RecoveryStopReason,
)
from tests.unit.test_outbox_drain import Harness


def recovery_services(
    harness: Harness,
) -> tuple[RecoverUnstartedOutboxService, RecoverExpiredOutboxService]:
    harness.repository.list_unstarted_candidates.return_value = tuple(
        event.event_id for event in harness.events
    )
    harness.repository.list_expired_candidates.return_value = tuple(
        ExpiredOutboxCandidate(event.event_id, uuid4()) for event in harness.events
    )
    harness.repository.recover_unstarted.return_value = harness.events[0]
    harness.repository.recover_expired.return_value = None
    return (
        RecoverUnstartedOutboxService(harness.transaction),
        RecoverExpiredOutboxService(harness.transaction, maximum_attempts=3),
    )


def test_one_budget_bounds_all_three_phases_and_cannot_be_reused_after_exhaustion() -> None:
    harness = Harness(25)
    unstarted, expired = recovery_services(harness)
    budget = OutboxSweepBudget(lambda: harness.elapsed)
    first = unstarted.recover(harness.context, budget=budget, max_targets=5)
    second = expired.recover(harness.context, budget=budget, max_targets=5)
    delivery = harness.service.drain(harness.context, budget=budget)
    assert (first.scanned, second.scanned, delivery.scanned) == (5, 5, 10)
    assert first.prepared == 5 and second.prepared == 0
    assert delivery.published == 10 and delivery.stop_reason is DrainStopReason.TARGET_LIMIT
    assert budget.used_targets == 20 and budget.remaining_targets == 0
    assert harness.repository.list_unstarted_candidates.call_args.kwargs["limit"] == 5
    assert harness.repository.list_expired_candidates.call_args.kwargs["limit"] == 5
    harness.repository.reset_mock()
    harness.publisher.reset_mock()
    assert (
        unstarted.recover(harness.context, budget=budget).stop_reason
        is RecoveryStopReason.TARGET_LIMIT
    )
    assert expired.recover(harness.context, budget=budget).scanned == 0
    assert harness.service.drain(harness.context, budget=budget).scanned == 0
    assert not harness.repository.mock_calls and not harness.publisher.mock_calls
    with pytest.raises(ValueError, match="exhausted"):
        budget.consume_target()


def test_shared_remaining_time_caps_readiness_and_publication_without_resetting() -> None:
    harness = Harness()
    unstarted, expired = recovery_services(harness)
    budget = OutboxSweepBudget(lambda: harness.elapsed)

    def prepare(*args: object, **kwargs: object) -> OutboxEvent:
        harness.elapsed = 29.5
        return harness.events[0]

    harness.repository.recover_unstarted.side_effect = prepare
    unstarted.recover(harness.context, budget=budget, max_targets=1)
    result = harness.service.drain(harness.context, budget=budget, max_targets=1)
    assert result.published == 1 and budget.used_targets == 2
    assert harness.publisher.is_ready.call_args.kwargs["timeout_seconds"] == 0.5
    assert harness.publisher.publish.call_args.kwargs["timeout_seconds"] == 0.5
    harness.elapsed = 30
    assert (
        expired.recover(harness.context, budget=budget).stop_reason is RecoveryStopReason.DEADLINE
    )
    harness.repository.list_expired_candidates.assert_not_called()
    assert budget.remaining_seconds == 0


def test_slow_snapshot_cannot_grant_a_fresh_deadline_to_later_phases() -> None:
    harness = Harness()
    unstarted, expired = recovery_services(harness)
    budget = OutboxSweepBudget(lambda: harness.elapsed)

    def snapshot(*args: object, **kwargs: object) -> tuple[object, ...]:
        harness.elapsed = 31
        return tuple(event.event_id for event in harness.events)

    harness.repository.list_unstarted_candidates.side_effect = snapshot
    assert (
        unstarted.recover(harness.context, budget=budget).stop_reason is RecoveryStopReason.DEADLINE
    )
    assert expired.recover(harness.context, budget=budget).scanned == 0
    assert (
        harness.service.drain(harness.context, budget=budget).stop_reason
        is DrainStopReason.DEADLINE
    )
    assert budget.used_targets == 0
    harness.repository.recover_unstarted.assert_not_called()
    harness.repository.list_expired_candidates.assert_not_called()
    harness.publisher.is_ready.assert_not_called()


def test_late_claim_still_counts_and_records_failure_after_shared_deadline() -> None:
    harness = Harness()
    budget = OutboxSweepBudget(lambda: harness.elapsed)

    def claim(*args: object, **kwargs: object) -> OutboxEvent:
        harness.elapsed = 30
        return harness.events[0]

    harness.repository.claim_next_due.side_effect = claim
    result = harness.service.drain(harness.context, budget=budget)
    assert result.stop_reason is DrainStopReason.DEADLINE and budget.used_targets == 1
    harness.repository.record_failure.assert_called_once()
    harness.publisher.publish.assert_not_called()


def test_database_snapshots_receive_only_the_remaining_target_allowance() -> None:
    harness = Harness(25)
    _, expired = recovery_services(harness)
    budget = OutboxSweepBudget(lambda: harness.elapsed)
    for _ in range(18):
        budget.consume_target()
    result = expired.recover(harness.context, budget=budget)
    assert result.scanned == 2 and budget.remaining_targets == 0
    assert harness.repository.list_expired_candidates.call_args.kwargs["limit"] == 2


@pytest.mark.parametrize("limit", [0, -1, 21])
@pytest.mark.parametrize("phase", ["unstarted", "expired", "delivery"])
def test_invalid_phase_limits_cannot_open_transactions_or_probe_broker(
    limit: int, phase: str
) -> None:
    harness = Harness()
    unstarted, expired = recovery_services(harness)
    action = {
        "unstarted": unstarted.recover,
        "expired": expired.recover,
        "delivery": harness.service.drain,
    }[phase]
    with pytest.raises(ValueError, match="between one and twenty"):
        action(harness.context, max_targets=limit)
    assert not harness.repository.mock_calls and not harness.publisher.mock_calls
