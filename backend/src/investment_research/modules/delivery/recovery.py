"""Bounded preparation of delivery intent for unstarted or expired processing work."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from time import monotonic

from investment_research.modules.delivery.application import OUTBOX_SWEEP_SECONDS, utc_now
from investment_research.modules.delivery.ports import (
    OUTBOX_RECOVERY_MAX_TARGETS,
    OutboxDeliveryUnitOfWorkFactory,
)
from investment_research.modules.identity.domain import WorkspaceContext


class RecoveryStopReason(StrEnum):
    SNAPSHOT_EXHAUSTED = "SNAPSHOT_EXHAUSTED"
    TARGET_LIMIT = "TARGET_LIMIT"
    DEADLINE = "DEADLINE"


@dataclass(frozen=True, slots=True)
class OutboxRecoveryResult:
    """Prepared includes an idempotently reused successor, never processing completion."""

    scanned: int
    prepared: int
    stop_reason: RecoveryStopReason


@dataclass(frozen=True, slots=True)
class RecoverUnstartedOutboxService:
    unit_of_work_factory: OutboxDeliveryUnitOfWorkFactory
    clock: Callable[[], datetime] = utc_now
    monotonic_clock: Callable[[], float] = monotonic

    def recover(self, context: WorkspaceContext) -> OutboxRecoveryResult:
        """Read one snapshot, then requalify each target in its own short transaction.

        The thirty-second budget prevents starting further work, not an in-flight SQL timeout.
        This prepares intent only; it neither publishes nor schedules a combined repair tick.
        """

        deadline = self.monotonic_clock() + OUTBOX_SWEEP_SECONDS
        with self.unit_of_work_factory() as work:
            candidates = work.delivery.list_unstarted_candidates(context, now=self.clock())
        scanned = prepared = 0
        for event_id in candidates[:OUTBOX_RECOVERY_MAX_TARGETS]:
            if self.monotonic_clock() >= deadline:
                return OutboxRecoveryResult(scanned, prepared, RecoveryStopReason.DEADLINE)
            with self.unit_of_work_factory() as work:
                successor = work.delivery.recover_unstarted(
                    context, event_id=event_id, now=self.clock()
                )
            scanned += 1
            prepared += successor is not None
        reason = (
            RecoveryStopReason.DEADLINE
            if self.monotonic_clock() >= deadline
            else RecoveryStopReason.TARGET_LIMIT
            if scanned == OUTBOX_RECOVERY_MAX_TARGETS
            else RecoveryStopReason.SNAPSHOT_EXHAUSTED
        )
        return OutboxRecoveryResult(scanned, prepared, reason)


@dataclass(frozen=True, slots=True)
class RecoverExpiredOutboxService:
    unit_of_work_factory: OutboxDeliveryUnitOfWorkFactory
    maximum_attempts: int
    clock: Callable[[], datetime] = utc_now
    monotonic_clock: Callable[[], float] = monotonic

    def __post_init__(self) -> None:
        if self.maximum_attempts < 1:
            raise ValueError("maximum_attempts must be positive")

    def recover(self, context: WorkspaceContext) -> OutboxRecoveryResult:
        """Recheck at most twenty observed execution tokens in separate transactions.

        The cooperative budget prevents further transactions, not in-flight SQL cancellation.
        Do not combine this and other independent sweeps into an unbounded runtime tick.
        """

        deadline = self.monotonic_clock() + OUTBOX_SWEEP_SECONDS
        with self.unit_of_work_factory() as work:
            candidates = work.delivery.list_expired_candidates(
                context, now=self.clock(), maximum_attempts=self.maximum_attempts
            )
        scanned = prepared = 0
        for candidate in candidates[:OUTBOX_RECOVERY_MAX_TARGETS]:
            if self.monotonic_clock() >= deadline:
                return OutboxRecoveryResult(scanned, prepared, RecoveryStopReason.DEADLINE)
            with self.unit_of_work_factory() as work:
                successor = work.delivery.recover_expired(
                    context,
                    event_id=candidate.event_id,
                    execution_lease_id=candidate.execution_lease_id,
                    now=self.clock(),
                    maximum_attempts=self.maximum_attempts,
                )
            scanned += 1
            prepared += successor is not None
        reason = (
            RecoveryStopReason.DEADLINE
            if self.monotonic_clock() >= deadline
            else RecoveryStopReason.TARGET_LIMIT
            if scanned == OUTBOX_RECOVERY_MAX_TARGETS
            else RecoveryStopReason.SNAPSHOT_EXHAUSTED
        )
        return OutboxRecoveryResult(scanned, prepared, reason)
