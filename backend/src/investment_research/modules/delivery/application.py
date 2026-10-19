"""Bounded, workspace-scoped outbox draining without broker calls in transactions."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from random import uniform
from time import monotonic

from investment_research.modules.delivery.domain import (
    OUTBOX_MAX_RETRY_DELAY_SECONDS,
    DeliveryErrorCategory,
    OutboxEvent,
    OutboxState,
)
from investment_research.modules.delivery.ports import (
    OutboxDeliveryUnitOfWorkFactory,
    OutboxPublicationInvalidError,
    OutboxPublicationUnavailableError,
    OutboxPublisher,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.contracts import ProcessingJobV1

OUTBOX_SWEEP_MAX_TARGETS = 20
OUTBOX_SWEEP_SECONDS = 30
OUTBOX_BROKER_OPERATION_SECONDS = 2


class DrainStopReason(StrEnum):
    EMPTY = "EMPTY"
    TARGET_LIMIT = "TARGET_LIMIT"
    DEADLINE = "DEADLINE"
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
    INVALID_PUBLICATION = "INVALID_PUBLICATION"


@dataclass(frozen=True, slots=True)
class OutboxDrainResult:
    """Content-free observations, not processing completion or proof that Redis retains tasks."""

    scanned: int
    claimed: int
    published: int
    rejected_receipts: int
    stop_reason: DrainStopReason


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class DrainOutboxService:
    unit_of_work_factory: OutboxDeliveryUnitOfWorkFactory
    publisher: OutboxPublisher
    clock: Callable[[], datetime] = utc_now
    monotonic_clock: Callable[[], float] = monotonic
    jitter: Callable[[float, float], float] = uniform

    def drain(self, context: WorkspaceContext) -> OutboxDrainResult:
        """Drain at most twenty rows in thirty seconds; never claim ahead of publication."""

        deadline = self.monotonic_clock() + OUTBOX_SWEEP_SECONDS
        scanned = claimed = published = rejected = 0
        reason = DrainStopReason.TARGET_LIMIT
        while scanned < OUTBOX_SWEEP_MAX_TARGETS:
            remaining = deadline - self.monotonic_clock()
            if remaining <= 0:
                reason = DrainStopReason.DEADLINE
                break
            try:
                ready = self.publisher.is_ready(
                    timeout_seconds=min(OUTBOX_BROKER_OPERATION_SECONDS, remaining)
                )
            except OutboxPublicationInvalidError:
                reason = DrainStopReason.INVALID_PUBLICATION
                break
            if not ready:
                reason = DrainStopReason.BROKER_UNAVAILABLE
                break
            if self.monotonic_clock() >= deadline:
                reason = DrainStopReason.DEADLINE
                break
            with self.unit_of_work_factory() as work:
                event = work.delivery.claim_next_due(context, now=self.clock())
            if event is None:
                reason = DrainStopReason.EMPTY
                break
            scanned += 1
            if event.state is not OutboxState.CLAIMED:
                continue
            claimed += 1
            assert event.lease_id is not None and event.lease_expires_at is not None
            remaining = min(
                deadline - self.monotonic_clock(),
                (event.lease_expires_at - self.clock()).total_seconds(),
            )
            if remaining <= 0:
                rejected += not self._record_failure(
                    context, event, DeliveryErrorCategory.DEPENDENCY_UNAVAILABLE
                )
                reason = DrainStopReason.DEADLINE
                break
            try:
                publication_deadline = self.monotonic_clock() + min(
                    OUTBOX_BROKER_OPERATION_SECONDS, remaining
                )
                self.publisher.publish(
                    ProcessingJobV1.create(
                        workspace_id=context.workspace_id, processing_run_id=event.target_id
                    ),
                    timeout_seconds=min(OUTBOX_BROKER_OPERATION_SECONDS, remaining),
                )
                if self.monotonic_clock() >= publication_deadline:
                    raise OutboxPublicationUnavailableError("publication deadline exceeded")
            except OutboxPublicationUnavailableError:
                rejected += not self._record_failure(
                    context, event, DeliveryErrorCategory.DEPENDENCY_UNAVAILABLE
                )
                reason = DrainStopReason.BROKER_UNAVAILABLE
                break
            except OutboxPublicationInvalidError:
                rejected += not self._record_failure(
                    context, event, DeliveryErrorCategory.INVALID_JOB
                )
                reason = DrainStopReason.INVALID_PUBLICATION
                break
            with self.unit_of_work_factory() as work:
                accepted = work.delivery.record_published(
                    context, event_id=event.event_id, lease_id=event.lease_id, now=self.clock()
                )
            published += accepted
            rejected += not accepted
        return OutboxDrainResult(scanned, claimed, published, rejected, reason)

    def _record_failure(
        self, context: WorkspaceContext, event: OutboxEvent, category: DeliveryErrorCategory
    ) -> bool:
        assert event.lease_id is not None
        upper_delay = min(OUTBOX_MAX_RETRY_DELAY_SECONDS, 2 ** (event.attempt_count - 1))
        delay = self.jitter(0, upper_delay)
        with self.unit_of_work_factory() as work:
            return work.delivery.record_failure(
                context,
                event_id=event.event_id,
                lease_id=event.lease_id,
                now=self.clock(),
                category=category,
                retry_delay_seconds=delay,
            )
