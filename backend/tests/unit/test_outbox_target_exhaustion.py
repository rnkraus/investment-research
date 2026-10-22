"""Execution exhaustion closes only due intent and never consumes a publication attempt."""

from datetime import timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest

from investment_research.infrastructure.database.outbox_unit_of_work import (
    create_outbox_delivery_unit_of_work_factory,
)
from investment_research.modules.delivery.domain import (
    DeliveryErrorCategory,
    OutboxState,
    OutboxTransitionError,
)
from tests.unit.test_outbox_contracts import NOW, make_event


@pytest.mark.parametrize("claimed", [False, True])
def test_target_exhaustion_preserves_publication_counts(claimed: bool) -> None:
    event = make_event()
    if claimed:
        event = event.claim(lease_id=uuid4(), claimed_at=NOW)
    before = event.model_dump()
    blocked = event.block_exhausted_target(now=NOW + timedelta(seconds=30))
    assert blocked.state is OutboxState.BLOCKED
    assert blocked.last_error_category is DeliveryErrorCategory.ATTEMPTS_EXHAUSTED
    assert blocked.attempt_count == event.attempt_count
    assert blocked.generation == event.generation and blocked.event_id == event.event_id
    assert blocked.lease_id is None and blocked.next_attempt_at is None
    assert event.model_dump() == before


def test_target_exhaustion_cannot_close_a_live_publication_claim_or_future_intent() -> None:
    event = make_event()
    with pytest.raises(OutboxTransitionError):
        event.block_exhausted_target(now=NOW - timedelta(seconds=1))
    claimed = event.claim(lease_id=uuid4(), claimed_at=NOW)
    with pytest.raises(OutboxTransitionError):
        claimed.block_exhausted_target(now=NOW + timedelta(seconds=29))
    for closed in (event.cancel(), event.block_exhausted_target(now=NOW)):
        with pytest.raises(OutboxTransitionError):
            closed.block_exhausted_target(now=NOW + timedelta(seconds=30))


@pytest.mark.parametrize("maximum_attempts", [0, -1])
def test_recovery_dispatch_rejects_invalid_profile_before_opening_database(
    maximum_attempts: int,
) -> None:
    sessions = Mock()
    with pytest.raises(ValueError, match="maximum_processing_attempts must be positive"):
        create_outbox_delivery_unit_of_work_factory(
            sessions, maximum_processing_attempts=maximum_attempts
        )
    sessions.assert_not_called()
