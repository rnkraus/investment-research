"""Expired execution adds a fence check without bypassing delivery grace or ceilings."""

from datetime import timedelta

import pytest

from investment_research.modules.delivery.domain import OutboxState, OutboxTransitionError
from tests.unit.test_outbox_contracts import NOW, make_event
from tests.unit.test_outbox_redelivery import published_event


@pytest.mark.parametrize(
    ("elapsed", "expiry", "eligible"),
    [(419, 360, False), (420, 421, False), (420, 420, True), (420, 360, True)],
)
def test_expired_redelivery_requires_both_grace_and_expiry(
    elapsed: int, expiry: int, eligible: bool
) -> None:
    original = published_event()
    before = original.model_dump()
    arguments = {
        "now": NOW + timedelta(seconds=elapsed),
        "execution_lease_expires_at": NOW + timedelta(seconds=expiry),
    }
    if not eligible:
        with pytest.raises(OutboxTransitionError):
            original.redeliver_expired(**arguments)
    else:
        successor = original.redeliver_expired(**arguments)
        assert successor == original.redeliver_unstarted(now=arguments["now"])
        assert successor.state is OutboxState.PENDING and successor.attempt_count == 0
    assert original.model_dump() == before


@pytest.mark.parametrize("field", ["now", "execution_lease_expires_at"])
def test_expired_redelivery_rejects_naive_time(field: str) -> None:
    arguments = {"now": NOW + timedelta(seconds=420), "execution_lease_expires_at": NOW}
    arguments[field] = arguments[field].replace(tzinfo=None)
    with pytest.raises(OutboxTransitionError):
        published_event().redeliver_expired(**arguments)


def test_expired_redelivery_cannot_revive_unpublished_or_cancelled_work() -> None:
    for event in (make_event(), published_event().cancel()):
        with pytest.raises(OutboxTransitionError):
            event.redeliver_expired(
                now=NOW + timedelta(seconds=420), execution_lease_expires_at=NOW
            )


def test_expired_redelivery_cannot_create_a_fourth_generation() -> None:
    event = published_event()
    for generation in (1, 2):
        now = NOW + timedelta(seconds=420 * generation)
        event = event.redeliver_expired(now=now, execution_lease_expires_at=NOW)
        lease_id = event.event_id
        event = event.claim(lease_id=lease_id, claimed_at=now).record_publication(
            lease_id=lease_id, completed_at=now
        )
    with pytest.raises(OutboxTransitionError):
        event.redeliver_expired(now=NOW + timedelta(seconds=1260), execution_lease_expires_at=NOW)
