"""Pure published-message-loss policy; canonical eligibility remains a repository concern."""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from investment_research.modules.delivery.domain import (
    OUTBOX_UNSTARTED_GRACE_SECONDS,
    DeliveryErrorCategory,
    OutboxEvent,
    OutboxState,
    OutboxTransitionError,
)
from tests.unit.test_outbox_contracts import NOW, make_event


def published_event() -> OutboxEvent:
    lease_id = uuid4()
    return (
        make_event()
        .claim(lease_id=lease_id, claimed_at=NOW)
        .record_publication(lease_id=lease_id, completed_at=NOW)
    )


def test_redelivery_preserves_prior_receipt_and_scoped_target() -> None:
    original = published_event()
    snapshot = original.model_dump()
    recovered_at = NOW + timedelta(seconds=420)
    next_event = original.redeliver_unstarted(now=recovered_at)
    assert OUTBOX_UNSTARTED_GRACE_SECONDS == 420
    assert original.model_dump() == snapshot
    assert next_event.workspace_id == original.workspace_id
    assert next_event.target_id == original.target_id
    assert next_event.kind is original.kind
    assert next_event.generation == original.generation + 1
    assert next_event.event_id != original.event_id
    assert next_event.state is OutboxState.PENDING
    assert next_event.attempt_count == 0
    assert next_event.created_at == next_event.next_attempt_at == recovered_at
    assert next_event.published_at is None
    assert next_event.lease_id is None
    assert next_event.lease_expires_at is None
    assert next_event.last_error_category is None
    repeated = original.redeliver_unstarted(now=recovered_at + timedelta(seconds=1))
    assert repeated.event_id == next_event.event_id


@pytest.mark.parametrize("seconds", [-1, 0, 419])
def test_redelivery_rejects_before_the_publication_grace_period(seconds: int) -> None:
    with pytest.raises(OutboxTransitionError):
        published_event().redeliver_unstarted(now=NOW + timedelta(seconds=seconds))


def test_grace_period_starts_at_publication_not_event_creation() -> None:
    lease_id = uuid4()
    event = (
        make_event()
        .claim(lease_id=lease_id, claimed_at=NOW + timedelta(seconds=60))
        .record_publication(lease_id=lease_id, completed_at=NOW + timedelta(seconds=61))
    )
    with pytest.raises(OutboxTransitionError):
        event.redeliver_unstarted(now=NOW + timedelta(seconds=420))
    assert event.redeliver_unstarted(now=NOW + timedelta(seconds=481)).generation == 1


@pytest.mark.parametrize("state", ["pending", "claimed", "retry", "blocked", "cancelled"])
def test_only_published_intent_can_request_a_new_unstarted_generation(state: str) -> None:
    event = make_event()
    lease_id = uuid4()
    if state in {"claimed", "retry", "blocked"}:
        event = event.claim(lease_id=lease_id, claimed_at=NOW)
    if state in {"retry", "blocked"}:
        event = event.record_failure(
            lease_id=lease_id,
            completed_at=NOW,
            retry_delay_seconds=1,
            category=(
                DeliveryErrorCategory.DEPENDENCY_UNAVAILABLE
                if state == "retry"
                else DeliveryErrorCategory.INVALID_JOB
            ),
        )
    if state == "cancelled":
        event = published_event().cancel()
    with pytest.raises(OutboxTransitionError):
        event.redeliver_unstarted(now=NOW + timedelta(seconds=420))


def test_three_generations_allow_at_most_nine_claims_without_rewriting_history() -> None:
    event = make_event()
    history = []
    claimed_at = NOW
    for generation in range(3):
        assert event.generation == generation
        for _ in range(3):
            lease_id = uuid4()
            event = event.claim(lease_id=lease_id, claimed_at=claimed_at)
            claimed_at += timedelta(seconds=30)
        event = event.record_publication(
            lease_id=lease_id, completed_at=claimed_at - timedelta(seconds=1)
        )
        history.append(event)
        claimed_at += timedelta(seconds=420)
        if generation < 2:
            event = event.redeliver_unstarted(now=claimed_at)
    assert len({item.event_id for item in history}) == 3
    assert sum(item.attempt_count for item in history) == 9
    assert all(item.state is OutboxState.PUBLISHED for item in history)
    with pytest.raises(OutboxTransitionError):
        event.redeliver_unstarted(now=claimed_at)


def test_redelivery_rejects_a_naive_clock() -> None:
    with pytest.raises(OutboxTransitionError):
        published_event().redeliver_unstarted(now=datetime(2026, 10, 20))
