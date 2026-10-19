"""Delivery fences and retry ceilings without database, broker, or provider calls."""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from investment_research.modules.delivery.domain import (
    DeliveryErrorCategory,
    OutboxState,
    OutboxTransitionError,
)
from tests.unit.test_outbox_contracts import NOW, make_event


def test_claim_and_publication_preserve_identity_without_claiming_execution_success() -> None:
    event = make_event()
    lease_id = uuid4()
    claim = event.claim(lease_id=lease_id, claimed_at=NOW)
    assert event.state is OutboxState.PENDING
    assert claim.event_id == event.event_id
    assert claim.target_id == event.target_id
    assert claim.generation == 0
    assert claim.attempt_count == 1
    assert claim.next_attempt_at is None
    assert claim.lease_expires_at == NOW + timedelta(seconds=30)
    published = claim.record_publication(lease_id=lease_id, completed_at=NOW)
    assert published.state is OutboxState.PUBLISHED
    assert published.published_at == NOW
    assert published.lease_id is None
    assert published.last_error_category is None
    with pytest.raises(OutboxTransitionError):
        published.claim(lease_id=uuid4(), claimed_at=NOW + timedelta(days=1))


@pytest.mark.parametrize("seconds", [-1, 30, 31])
def test_publication_rejects_before_claim_or_at_expiry(seconds: int) -> None:
    claim = make_event().claim(lease_id=uuid4(), claimed_at=NOW)
    assert claim.lease_id is not None
    with pytest.raises(OutboxTransitionError):
        claim.record_publication(
            lease_id=claim.lease_id, completed_at=NOW + timedelta(seconds=seconds)
        )


def test_live_claim_and_foreign_owner_are_rejected() -> None:
    claim = make_event().claim(lease_id=uuid4(), claimed_at=NOW)
    with pytest.raises(OutboxTransitionError):
        claim.claim(lease_id=uuid4(), claimed_at=NOW + timedelta(seconds=29))
    with pytest.raises(OutboxTransitionError):
        claim.record_publication(lease_id=uuid4(), completed_at=NOW)
    with pytest.raises(OutboxTransitionError):
        claim.record_failure(
            lease_id=uuid4(),
            completed_at=NOW,
            category=DeliveryErrorCategory.DEPENDENCY_UNAVAILABLE,
            retry_delay_seconds=1,
        )


def test_expired_claim_can_be_reclaimed_but_not_past_the_third_attempt() -> None:
    event = make_event()
    for attempt in range(1, 4):
        event = event.claim(
            lease_id=uuid4(), claimed_at=NOW + timedelta(seconds=(attempt - 1) * 30)
        )
        assert event.attempt_count == attempt
    assert event.lease_id is not None
    with pytest.raises(OutboxTransitionError):
        event.claim(lease_id=uuid4(), claimed_at=NOW + timedelta(seconds=90))
    with pytest.raises(OutboxTransitionError):
        event.block_expired_final_claim(now=NOW + timedelta(seconds=89))
    blocked = event.block_expired_final_claim(now=NOW + timedelta(seconds=90))
    assert blocked.state is OutboxState.BLOCKED
    assert blocked.last_error_category is DeliveryErrorCategory.ATTEMPTS_EXHAUSTED
    assert blocked.attempt_count == 3
    assert blocked.generation == 0
    assert blocked.lease_id is None


def test_transient_failure_retries_only_when_due_and_stops_at_three_claims() -> None:
    event = make_event()
    for attempt in range(1, 4):
        claimed_at = NOW + timedelta(seconds=(attempt - 1) * 10)
        lease_id = uuid4()
        event = event.claim(lease_id=lease_id, claimed_at=claimed_at)
        event = event.record_failure(
            lease_id=lease_id,
            completed_at=claimed_at,
            category=DeliveryErrorCategory.DEPENDENCY_UNAVAILABLE,
            retry_delay_seconds=10,
        )
        assert event.attempt_count == attempt
        if attempt < 3:
            assert event.state is OutboxState.RETRY_WAIT
            with pytest.raises(OutboxTransitionError):
                event.claim(lease_id=uuid4(), claimed_at=claimed_at + timedelta(seconds=9))
        else:
            assert event.state is OutboxState.BLOCKED
            assert event.next_attempt_at is None
            assert event.last_error_category is DeliveryErrorCategory.ATTEMPTS_EXHAUSTED


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        (DeliveryErrorCategory.INVALID_JOB, OutboxState.BLOCKED),
        (DeliveryErrorCategory.SOURCE_INELIGIBLE, OutboxState.CANCELLED),
    ],
)
def test_permanent_failure_does_not_schedule_a_retry(
    category: DeliveryErrorCategory, expected: OutboxState
) -> None:
    lease_id = uuid4()
    event = (
        make_event()
        .claim(lease_id=lease_id, claimed_at=NOW)
        .record_failure(
            lease_id=lease_id, completed_at=NOW, category=category, retry_delay_seconds=0
        )
    )
    assert event.state is expected
    assert event.attempt_count == 1
    assert event.last_error_category is category
    assert event.next_attempt_at is None


@pytest.mark.parametrize("delay", [-1, 30.1, float("inf"), float("nan")])
def test_retry_delay_cannot_exceed_the_accepted_bound(delay: float) -> None:
    lease_id = uuid4()
    event = make_event().claim(lease_id=lease_id, claimed_at=NOW)
    with pytest.raises(OutboxTransitionError):
        event.record_failure(
            lease_id=lease_id,
            completed_at=NOW,
            category=DeliveryErrorCategory.DEPENDENCY_UNAVAILABLE,
            retry_delay_seconds=delay,
        )


def test_naive_clock_and_nonfinal_expiry_cannot_modify_delivery() -> None:
    with pytest.raises(OutboxTransitionError):
        make_event().claim(lease_id=uuid4(), claimed_at=datetime(2026, 10, 19))
    event = make_event().claim(lease_id=uuid4(), claimed_at=NOW)
    with pytest.raises(OutboxTransitionError):
        event.block_expired_final_claim(now=NOW + timedelta(seconds=30))


def test_cancellation_keeps_prior_publication_receipt() -> None:
    lease_id = uuid4()
    event = (
        make_event()
        .claim(lease_id=lease_id, claimed_at=NOW)
        .record_publication(lease_id=lease_id, completed_at=NOW)
    )
    cancelled = event.cancel()
    assert cancelled.state is OutboxState.CANCELLED
    assert cancelled.published_at == NOW
    assert cancelled.attempt_count == 1
    assert cancelled.event_id == event.event_id
