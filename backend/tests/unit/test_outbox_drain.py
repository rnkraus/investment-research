"""Deterministic sweep/deadline checks; no broker, provider, or database required."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest

from investment_research.modules.delivery.application import DrainOutboxService, DrainStopReason
from investment_research.modules.delivery.domain import DeliveryErrorCategory, OutboxEvent
from investment_research.modules.delivery.ports import (
    OutboxDeliveryRepository,
    OutboxDeliveryUnitOfWork,
    OutboxPublicationInvalidError,
    OutboxPublicationUnavailableError,
)
from investment_research.modules.identity.domain import WorkspaceContext
from tests.unit.test_outbox_contracts import NOW


@dataclass
class Repositories:
    delivery: OutboxDeliveryRepository


class Harness:
    def __init__(self, count: int = 1) -> None:
        self.context = WorkspaceContext(user_id=uuid4(), workspace_id=uuid4())
        self.elapsed = 0.0
        self.active_transactions = 0
        self.events = [
            OutboxEvent.for_processing(
                workspace_id=self.context.workspace_id, processing_run_id=uuid4(), created_at=NOW
            ).claim(lease_id=uuid4(), claimed_at=NOW)
            for _ in range(count)
        ]
        self.repository = Mock(spec=OutboxDeliveryRepository)
        self.repository.claim_next_due.side_effect = [*self.events, None]
        self.repository.record_published.return_value = True
        self.repository.record_failure.return_value = True
        self.publisher = Mock()
        self.publisher.is_ready.side_effect = self.ready
        self.publisher.publish.side_effect = self.publish
        self.service = DrainOutboxService(
            unit_of_work_factory=self.transaction,
            publisher=self.publisher,
            clock=lambda: NOW + timedelta(seconds=self.elapsed),
            monotonic_clock=lambda: self.elapsed,
            jitter=lambda low, high: high,
        )

    @contextmanager
    def transaction(self) -> Iterator[OutboxDeliveryUnitOfWork]:
        self.active_transactions += 1
        try:
            yield Repositories(self.repository)
        finally:
            self.active_transactions -= 1

    def ready(self, *, timeout_seconds: float) -> bool:
        assert self.active_transactions == 0
        assert 0 < timeout_seconds <= 2
        return True

    def publish(self, job: object, *, timeout_seconds: float) -> None:
        assert self.active_transactions == 0
        assert 0 < timeout_seconds <= 2


def test_publication_is_outside_transactions_and_receipt_is_separate() -> None:
    harness = Harness()
    result = harness.service.drain(harness.context)
    assert (result.scanned, result.claimed, result.published, result.rejected_receipts) == (
        1,
        1,
        1,
        0,
    )
    assert result.stop_reason is DrainStopReason.EMPTY
    job = harness.publisher.publish.call_args.args[0]
    assert job.workspace_id == harness.context.workspace_id
    assert job.processing_run_id == harness.events[0].target_id
    assert set(job.model_dump()) == {
        "schema_version",
        "workspace_id",
        "processing_run_id",
        "idempotency_key",
    }
    assert (
        harness.repository.record_published.call_args.kwargs["lease_id"]
        == harness.events[0].lease_id
    )


def test_broker_outage_does_not_claim_or_consume_an_attempt() -> None:
    harness = Harness()
    harness.publisher.is_ready.side_effect = None
    harness.publisher.is_ready.return_value = False
    result = harness.service.drain(harness.context)
    assert result.stop_reason is DrainStopReason.BROKER_UNAVAILABLE
    assert result.scanned == result.claimed == 0
    harness.repository.claim_next_due.assert_not_called()
    harness.publisher.publish.assert_not_called()


def test_slow_readiness_cannot_claim_after_the_sweep_deadline() -> None:
    harness = Harness()

    def ready(*, timeout_seconds: float) -> bool:
        harness.elapsed = 30
        return True

    harness.publisher.is_ready.side_effect = ready
    result = harness.service.drain(harness.context)
    assert result.stop_reason is DrainStopReason.DEADLINE
    harness.repository.claim_next_due.assert_not_called()


def test_sweep_stops_at_twenty_rows_including_nonpublication_outcomes() -> None:
    harness = Harness(count=25)
    harness.repository.claim_next_due.side_effect = [event.cancel() for event in harness.events]
    result = harness.service.drain(harness.context)
    assert result.scanned == 20
    assert result.claimed == result.published == 0
    assert result.stop_reason is DrainStopReason.TARGET_LIMIT
    harness.publisher.publish.assert_not_called()


@pytest.mark.parametrize(
    ("failure", "category", "reason"),
    [
        (
            OutboxPublicationUnavailableError,
            DeliveryErrorCategory.DEPENDENCY_UNAVAILABLE,
            DrainStopReason.BROKER_UNAVAILABLE,
        ),
        (
            OutboxPublicationInvalidError,
            DeliveryErrorCategory.INVALID_JOB,
            DrainStopReason.INVALID_PUBLICATION,
        ),
    ],
)
def test_publish_failure_persists_safe_reason_and_stops_before_claiming_more_work(
    failure: type[Exception], category: DeliveryErrorCategory, reason: DrainStopReason
) -> None:
    harness = Harness(count=2)
    harness.publisher.publish.side_effect = failure("synthetic untrusted details")
    result = harness.service.drain(harness.context)
    assert result.stop_reason is reason
    assert result.claimed == 1 and result.published == 0
    assert harness.repository.claim_next_due.call_count == 1
    failure_arguments = harness.repository.record_failure.call_args.kwargs
    assert failure_arguments["category"] is category
    assert failure_arguments["retry_delay_seconds"] == 1
    assert "synthetic" not in repr(result)


def test_late_success_is_an_uncertain_publication_not_a_successful_receipt() -> None:
    harness = Harness()

    def late_publish(job: object, *, timeout_seconds: float) -> None:
        harness.elapsed += timeout_seconds

    harness.publisher.publish.side_effect = late_publish
    result = harness.service.drain(harness.context)
    assert result.published == 0
    assert result.stop_reason is DrainStopReason.BROKER_UNAVAILABLE
    harness.repository.record_published.assert_not_called()
    harness.repository.record_failure.assert_called_once()


def test_remaining_sweep_time_caps_the_last_publication() -> None:
    harness = Harness()

    def ready(*, timeout_seconds: float) -> bool:
        harness.elapsed = 29.5
        return True

    harness.publisher.is_ready.side_effect = ready
    result = harness.service.drain(harness.context)
    assert result.published == 1
    assert harness.publisher.publish.call_args.kwargs["timeout_seconds"] == 0.5


def test_expired_claim_after_commit_never_reaches_the_broker() -> None:
    harness = Harness()

    def slow_claim(*args: object, **kwargs: object) -> OutboxEvent:
        harness.elapsed = 30
        return harness.events[0]

    harness.repository.claim_next_due.side_effect = slow_claim
    harness.repository.record_failure.return_value = False
    result = harness.service.drain(harness.context)
    assert result.stop_reason is DrainStopReason.DEADLINE
    assert result.rejected_receipts == 1
    harness.publisher.publish.assert_not_called()


def test_rejected_receipt_is_not_counted_as_publication_success() -> None:
    harness = Harness()
    harness.repository.record_published.return_value = False
    result = harness.service.drain(harness.context)
    assert result.published == 0 and result.rejected_receipts == 1


def test_programming_and_commit_errors_are_not_classified_as_broker_outages() -> None:
    harness = Harness()
    harness.publisher.publish.side_effect = TypeError("synthetic programming failure")
    with pytest.raises(TypeError):
        harness.service.drain(harness.context)
    harness.repository.record_failure.assert_not_called()
    harness = Harness()
    harness.repository.record_published.side_effect = OSError(
        "synthetic lost commit acknowledgement"
    )
    with pytest.raises(OSError):
        harness.service.drain(harness.context)
    harness.publisher.publish.assert_called_once()
    harness.repository.record_failure.assert_not_called()
