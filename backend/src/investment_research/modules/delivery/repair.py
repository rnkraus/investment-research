"""One opt-in repair pass; runtime scheduling and activation remain separate concerns."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from random import uniform
from time import monotonic

from investment_research.modules.delivery.application import (
    DrainOutboxService,
    OutboxDrainResult,
    utc_now,
)
from investment_research.modules.delivery.budget import OUTBOX_SWEEP_MAX_TARGETS, OutboxSweepBudget
from investment_research.modules.delivery.ports import (
    OutboxDeliveryUnitOfWorkFactory,
    OutboxPublisher,
)
from investment_research.modules.delivery.recovery import (
    OutboxRecoveryResult,
    RecoverExpiredOutboxService,
    RecoverUnstartedOutboxService,
)
from investment_research.modules.identity.domain import WorkspaceContext

# Each preparation category gets one quarter; at least half remains for returned claims.
OUTBOX_REPAIR_PREPARATION_TARGETS = OUTBOX_SWEEP_MAX_TARGETS // 4


@dataclass(frozen=True, slots=True)
class OutboxRepairResult:
    """Content-free phase observations, never proof of completed processing."""

    unstarted: OutboxRecoveryResult
    expired: OutboxRecoveryResult
    delivery: OutboxDrainResult
    used_targets: int


@dataclass(frozen=True, slots=True)
class RepairOutboxService:
    """Compose fenced recovery and delivery with one work allowance and deadline.

    The composition root must configure the transaction factory with this same processing-profile
    attempt ceiling to enable expired-run dispatch. No scheduler or normal runtime is wired here.
    """

    unit_of_work_factory: OutboxDeliveryUnitOfWorkFactory
    publisher: OutboxPublisher
    maximum_attempts: int
    clock: Callable[[], datetime] = utc_now
    monotonic_clock: Callable[[], float] = monotonic
    jitter: Callable[[float, float], float] = uniform

    def __post_init__(self) -> None:
        if self.maximum_attempts < 1:
            raise ValueError("maximum_attempts must be positive")

    def repair(self, context: WorkspaceContext) -> OutboxRepairResult:
        """Prepare at most five targets per category, then drain the remaining allowance.

        Empty categories leave slots for delivery. Preparation cannot consume every work slot,
        but slow SQL can still consume the shared time budget. Preparing pending intent during a
        broker outage is safe: only readiness-qualified publication claims consume delivery
        attempts.
        """

        budget = OutboxSweepBudget(self.monotonic_clock)
        unstarted = RecoverUnstartedOutboxService(
            self.unit_of_work_factory, clock=self.clock
        ).recover(context, budget=budget, max_targets=OUTBOX_REPAIR_PREPARATION_TARGETS)
        expired = RecoverExpiredOutboxService(
            self.unit_of_work_factory, self.maximum_attempts, clock=self.clock
        ).recover(context, budget=budget, max_targets=OUTBOX_REPAIR_PREPARATION_TARGETS)
        delivery = DrainOutboxService(
            self.unit_of_work_factory,
            self.publisher,
            clock=self.clock,
            jitter=self.jitter,
        ).drain(context, budget=budget)
        return OutboxRepairResult(unstarted, expired, delivery, budget.used_targets)
