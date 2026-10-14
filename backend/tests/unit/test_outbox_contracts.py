"""Deterministic, content-free delivery contracts without broker or database calls."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_research.modules.delivery.domain import (
    DeliveryErrorCategory,
    OutboxEvent,
    OutboxKind,
    OutboxState,
    outbox_event_id,
)

NOW = datetime(2026, 10, 14, tzinfo=UTC)


def make_event() -> OutboxEvent:
    return OutboxEvent.for_processing(
        workspace_id=uuid4(), processing_run_id=uuid4(), created_at=NOW
    )


def test_initial_intent_has_stable_workspace_target_and_generation_identity() -> None:
    event = make_event()
    replay = OutboxEvent.for_processing(
        workspace_id=event.workspace_id, processing_run_id=event.target_id, created_at=NOW
    )
    assert replay == event
    assert event.state is OutboxState.PENDING
    assert event.attempt_count == 0
    assert event.next_attempt_at == NOW
    assert event.event_id != outbox_event_id(uuid4(), event.kind, event.target_id, 0)
    assert event.event_id != outbox_event_id(event.workspace_id, event.kind, uuid4(), 0)
    assert event.event_id != outbox_event_id(event.workspace_id, event.kind, event.target_id, 1)
    assert event.kind is OutboxKind.PROCESS_DOCUMENT


@pytest.mark.parametrize(
    "changes",
    [
        {"event_id": uuid4()},
        {"target_id": uuid4()},
        {"workspace_id": uuid4()},
        {"generation": 3},
        {"attempt_count": 4},
        {"created_at": datetime(2026, 10, 14)},
        {"next_attempt_at": None},
        {"next_attempt_at": NOW - timedelta(seconds=1)},
        {"published_at": NOW},
        {"lease_id": uuid4()},
        {"lease_expires_at": NOW + timedelta(seconds=30)},
        {"state": "CLAIMED"},
        {"state": "PUBLISHED"},
        {"state": "BLOCKED", "next_attempt_at": None},
        {"last_error_category": "raw-provider-details"},
        {"source_text": "Untrusted document content"},
        {"schema_version": "outbox-event.v2"},
    ],
)
def test_incoherent_or_content_carrying_intent_is_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        OutboxEvent.model_validate(make_event().model_dump() | changes)


def test_claim_publication_and_cancellation_metadata_preserve_receipt() -> None:
    initial = make_event().model_dump()
    claimed = OutboxEvent.model_validate(
        initial
        | {
            "state": OutboxState.CLAIMED,
            "attempt_count": 1,
            "next_attempt_at": None,
            "lease_id": uuid4(),
            "lease_expires_at": NOW + timedelta(seconds=30),
        }
    )
    published = OutboxEvent.model_validate(
        claimed.model_dump()
        | {
            "state": OutboxState.PUBLISHED,
            "lease_id": None,
            "lease_expires_at": None,
            "published_at": NOW + timedelta(seconds=1),
        }
    )
    cancelled = OutboxEvent.model_validate(
        published.model_dump()
        | {
            "state": OutboxState.CANCELLED,
            "last_error_category": DeliveryErrorCategory.SOURCE_INELIGIBLE,
        }
    )
    assert cancelled.published_at == published.published_at
    assert cancelled.attempt_count == published.attempt_count


@pytest.mark.parametrize("attempt_count", [0, 3])
def test_retry_wait_requires_remaining_attempts(attempt_count: int) -> None:
    with pytest.raises(ValidationError):
        OutboxEvent.model_validate(
            make_event().model_dump()
            | {
                "state": OutboxState.RETRY_WAIT,
                "attempt_count": attempt_count,
                "last_error_category": DeliveryErrorCategory.DEPENDENCY_UNAVAILABLE,
            }
        )
