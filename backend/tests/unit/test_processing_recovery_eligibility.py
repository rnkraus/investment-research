"""Expired-execution policy never steals live work or resets canonical attempt history."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from investment_research.modules.processing.domain import ProcessingRun, ProcessingState
from tests.unit.test_processing_persistence import LEASE_EXPIRES_AT, make_run

ACTIVE_CASES = (
    (ProcessingState.PARSING, False),
    (ProcessingState.PARSING, True),
    (ProcessingState.LEXICAL_READY, True),
    (ProcessingState.INDEXING, True),
)


def active_run(state: ProcessingState, dense: bool) -> ProcessingRun:
    return make_run(
        state,
        dense_indexing_enabled=dense,
        active_lexical_ready=state is ProcessingState.LEXICAL_READY,
    )


@pytest.mark.parametrize(("state", "dense"), ACTIVE_CASES)
@pytest.mark.parametrize("seconds", [-1, 0, 1, 600])
def test_recovery_requires_expiry_not_just_an_old_publication(
    state: ProcessingState, dense: bool, seconds: int
) -> None:
    run = active_run(state, dense)
    before = run.model_dump()
    assert run.can_reclaim_expired(
        now=LEASE_EXPIRES_AT + timedelta(seconds=seconds), maximum_attempts=3
    ) is (seconds >= 0)
    assert run.model_dump() == before


@pytest.mark.parametrize(("state", "dense"), ACTIVE_CASES)
@pytest.mark.parametrize("attempts", [3, 4])
def test_expiry_does_not_grant_another_attempt_after_exhaustion(
    state: ProcessingState, dense: bool, attempts: int
) -> None:
    run = ProcessingRun.model_validate(
        active_run(state, dense).model_dump() | {"attempt_count": attempts}
    )
    assert not run.can_reclaim_expired(now=LEASE_EXPIRES_AT + timedelta(days=1), maximum_attempts=3)
    assert run.attempt_count == attempts


@pytest.mark.parametrize(
    ("state", "dense"),
    [
        (ProcessingState.CAPTURED, False),
        (ProcessingState.CAPTURED, True),
        (ProcessingState.LEXICAL_READY, False),
        (ProcessingState.LEXICAL_READY, True),
        (ProcessingState.SEARCHABLE, True),
        (ProcessingState.FAILED, False),
        (ProcessingState.FAILED, True),
    ],
)
def test_unstarted_and_terminal_runs_are_not_expired_execution_work(
    state: ProcessingState, dense: bool
) -> None:
    run = make_run(state, dense_indexing_enabled=dense)
    assert not run.can_reclaim_expired(now=LEASE_EXPIRES_AT + timedelta(days=1), maximum_attempts=3)


@pytest.mark.parametrize(("state", "dense"), ACTIVE_CASES)
def test_new_execution_fence_invalidates_the_previous_eligibility_snapshot(
    state: ProcessingState, dense: bool
) -> None:
    previous = active_run(state, dense)
    assert previous.can_reclaim_expired(now=LEASE_EXPIRES_AT, maximum_attempts=3)
    new_lease = uuid4()
    claim = (
        previous.claim_for_processing
        if state is ProcessingState.PARSING
        else previous.claim_for_indexing
    )
    reclaimed = claim(
        lease_id=new_lease,
        claimed_at=LEASE_EXPIRES_AT,
        lease_expires_at=LEASE_EXPIRES_AT + timedelta(seconds=360),
        maximum_attempts=3,
    )
    assert not reclaimed.can_reclaim_expired(now=LEASE_EXPIRES_AT, maximum_attempts=3)
    assert reclaimed.lease_id == new_lease and reclaimed.attempt_count == 2
    assert previous.attempt_count == 1 and previous.lease_id != new_lease
    assert reclaimed.state is previous.state


def test_equivalent_timezone_offsets_have_the_same_expiry_boundary() -> None:
    run = active_run(ProcessingState.PARSING, False)
    local_expiry = LEASE_EXPIRES_AT.astimezone(timezone(timedelta(hours=2)))
    assert run.can_reclaim_expired(now=local_expiry, maximum_attempts=3)


def test_recovery_rejects_a_naive_clock() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        active_run(ProcessingState.PARSING, False).can_reclaim_expired(
            now=datetime(2026, 10, 22), maximum_attempts=3
        )


@pytest.mark.parametrize("maximum_attempts", [0, -1])
def test_recovery_rejects_an_invalid_attempt_policy(maximum_attempts: int) -> None:
    with pytest.raises(ValueError, match="maximum_attempts must be positive"):
        active_run(ProcessingState.PARSING, False).can_reclaim_expired(
            now=LEASE_EXPIRES_AT, maximum_attempts=maximum_attempts
        )
