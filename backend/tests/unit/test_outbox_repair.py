"""Opt-in orchestration preserves shared bounds, scope, and publication fencing."""

from datetime import timedelta

import pytest

from investment_research.modules.delivery.application import DrainStopReason
from investment_research.modules.delivery.domain import OutboxEvent
from investment_research.modules.delivery.recovery import RecoveryStopReason
from investment_research.modules.delivery.repair import RepairOutboxService
from tests.unit.test_outbox_contracts import NOW
from tests.unit.test_outbox_drain import Harness
from tests.unit.test_outbox_sweep_budget import recovery_services


def repair_service(harness: Harness) -> RepairOutboxService:
    recovery_services(harness)
    return RepairOutboxService(
        harness.transaction,
        harness.publisher,
        maximum_attempts=3,
        clock=lambda: NOW + timedelta(seconds=harness.elapsed),
        monotonic_clock=lambda: harness.elapsed,
        jitter=lambda low, high: high,
    )


def test_preparation_reserves_half_the_work_allowance_for_delivery() -> None:
    harness = Harness(25)
    result = repair_service(harness).repair(harness.context)
    assert (result.unstarted.scanned, result.expired.scanned, result.delivery.scanned) == (5, 5, 10)
    assert result.unstarted.prepared == 5 and result.expired.prepared == 0
    assert result.delivery.published == 10 and result.used_targets == 20
    assert result.delivery.stop_reason is DrainStopReason.TARGET_LIMIT
    assert harness.repository.list_unstarted_candidates.call_args.kwargs["limit"] == 5
    expired_arguments = harness.repository.list_expired_candidates.call_args
    assert expired_arguments.kwargs["limit"] == 5
    assert expired_arguments.kwargs["maximum_attempts"] == 3
    for call in harness.repository.mock_calls:
        assert call.args[0] == harness.context
    assert harness.active_transactions == 0


def test_empty_preparation_leaves_the_full_allowance_for_delivery() -> None:
    harness = Harness(25)
    service = repair_service(harness)
    harness.repository.list_unstarted_candidates.return_value = ()
    harness.repository.list_expired_candidates.return_value = ()
    result = service.repair(harness.context)
    assert result.unstarted.scanned == result.expired.scanned == 0
    assert result.delivery.published == result.used_targets == 20


def test_completely_empty_pass_is_not_reported_as_processing_success() -> None:
    harness = Harness()
    service = repair_service(harness)
    harness.repository.list_unstarted_candidates.return_value = ()
    harness.repository.list_expired_candidates.return_value = ()
    harness.repository.claim_next_due.side_effect = [None]
    result = service.repair(harness.context)
    assert result.used_targets == result.delivery.published == 0
    assert result.delivery.stop_reason is DrainStopReason.EMPTY
    assert result.unstarted.stop_reason is RecoveryStopReason.SNAPSHOT_EXHAUSTED
    assert result.expired.stop_reason is RecoveryStopReason.SNAPSHOT_EXHAUSTED


def test_slow_preparation_cannot_restart_the_deadline_for_later_phases() -> None:
    harness = Harness()
    service = repair_service(harness)

    def prepare(*args: object, **kwargs: object) -> OutboxEvent:
        harness.elapsed = 30
        return harness.events[0]

    harness.repository.recover_unstarted.side_effect = prepare
    result = service.repair(harness.context)
    assert result.used_targets == result.unstarted.prepared == 1
    assert result.unstarted.stop_reason is RecoveryStopReason.DEADLINE
    assert result.expired.stop_reason is RecoveryStopReason.DEADLINE
    assert result.delivery.stop_reason is DrainStopReason.DEADLINE
    harness.repository.list_expired_candidates.assert_not_called()
    harness.repository.claim_next_due.assert_not_called()
    harness.publisher.is_ready.assert_not_called()


def test_partial_deadline_reaches_broker_without_a_phase_local_reset() -> None:
    harness = Harness()
    service = repair_service(harness)
    harness.repository.list_expired_candidates.return_value = ()

    def prepare(*args: object, **kwargs: object) -> OutboxEvent:
        harness.elapsed = 29.5
        return harness.events[0]

    harness.repository.recover_unstarted.side_effect = prepare
    result = service.repair(harness.context)
    assert result.delivery.published == 1 and result.used_targets == 2
    assert harness.publisher.publish.call_args.kwargs["timeout_seconds"] == 0.5
    assert harness.publisher.is_ready.call_args.kwargs["timeout_seconds"] == 0.5


def test_broker_outage_can_prepare_intent_but_cannot_claim_publication() -> None:
    harness = Harness()
    service = repair_service(harness)
    harness.publisher.is_ready.side_effect = None
    harness.publisher.is_ready.return_value = False
    result = service.repair(harness.context)
    assert result.unstarted.prepared == 1 and result.used_targets == 2
    assert result.delivery.stop_reason is DrainStopReason.BROKER_UNAVAILABLE
    assert result.delivery.claimed == result.delivery.published == 0
    harness.repository.claim_next_due.assert_not_called()
    harness.repository.record_published.assert_not_called()
    harness.publisher.publish.assert_not_called()


def test_unexpected_database_failure_propagates_instead_of_reporting_success() -> None:
    harness = Harness()
    service = repair_service(harness)
    harness.repository.recover_unstarted.side_effect = RuntimeError("synthetic failure")
    with pytest.raises(RuntimeError, match="synthetic failure"):
        service.repair(harness.context)
    assert harness.active_transactions == 0
    harness.repository.list_expired_candidates.assert_not_called()
    harness.publisher.is_ready.assert_not_called()


@pytest.mark.parametrize("maximum_attempts", [0, -1])
def test_invalid_processing_ceiling_fails_before_any_transaction(maximum_attempts: int) -> None:
    harness = Harness()
    with pytest.raises(ValueError, match="positive"):
        RepairOutboxService(harness.transaction, harness.publisher, maximum_attempts)
    assert not harness.repository.mock_calls and not harness.publisher.mock_calls
