"""Application-owned outbox persistence within caller-owned transactions."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from investment_research.modules.delivery.budget import OUTBOX_SWEEP_MAX_TARGETS
from investment_research.modules.delivery.domain import DeliveryErrorCategory, OutboxEvent
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.contracts import ProcessingJobV1

OUTBOX_RECOVERY_MAX_TARGETS = OUTBOX_SWEEP_MAX_TARGETS


@dataclass(frozen=True, slots=True)
class ExpiredOutboxCandidate:
    """Content-free observation, not authorization to reclaim a subsequently renewed lease."""

    event_id: UUID
    execution_lease_id: UUID


class OutboxScopeError(ValueError):
    """Reject an unavailable or incompatible target without exposing identifiers."""


class OutboxPublicationUnavailableError(Exception):
    """A bounded broker operation failed or its outcome is uncertain."""


class OutboxPublicationInvalidError(Exception):
    """The configured publication path or typed job is not usable without correction."""


class OutboxPublisher(Protocol):
    def is_ready(self, *, timeout_seconds: float) -> bool:
        """Check the broker before consuming an attempt, within one wall-clock budget."""

    def publish(self, job: ProcessingJobV1, *, timeout_seconds: float) -> None:
        """Publish once without hidden retries, or raise a safe application-owned error."""


class OutboxRepairJobExecutor(Protocol):
    def execute_repair(self) -> None:
        """Resolve the trusted worker context and run one bounded pass, never a payload scope."""


class OutboxRepository(Protocol):
    def add_processing_event(self, context: WorkspaceContext, event: OutboxEvent) -> OutboxEvent:
        """Stage one initial intent idempotently; never commit or publish it here."""

    def get_by_id(self, context: WorkspaceContext, event_id: UUID) -> OutboxEvent | None:
        """Return the same absence for unknown and foreign event identifiers."""

    def cancel_processing_events(
        self, context: WorkspaceContext, processing_run_ids: tuple[UUID, ...]
    ) -> None:
        """Detach content-free delivery history inside the canonical purge transaction."""


class OutboxDeliveryRepository(Protocol):
    """Short delivery transactions, separate from broker readiness and publication."""

    def list_unstarted_candidates(
        self, context: WorkspaceContext, *, now: datetime, limit: int = OUTBOX_RECOVERY_MAX_TARGETS
    ) -> tuple[UUID, ...]:
        """Snapshot at most twenty due latest published events for eligible captured runs.

        Include final generations for bounded exhaustion closure, but exclude newer intent.
        This read does not authorize mutation; recover_unstarted must lock/recheck each target.
        """

    def list_expired_candidates(
        self,
        context: WorkspaceContext,
        *,
        now: datetime,
        maximum_attempts: int,
        limit: int = OUTBOX_RECOVERY_MAX_TARGETS,
    ) -> tuple[ExpiredOutboxCandidate, ...]:
        """Snapshot at most twenty eligible latest published events and execution tokens.

        Require publication grace and expired unfinished execution. Include exhausted targets
        for closure, not another attempt. Lock/recheck every observed token before mutation.
        """

    def recover_unstarted(
        self, context: WorkspaceContext, *, event_id: UUID, now: datetime
    ) -> OutboxEvent | None:
        """Insert one next generation after canonical eligibility and latest-event checks.

        An exhausted latest publication becomes BLOCKED with its receipt retained. A repeat
        may return an existing direct successor unchanged. Unknown, foreign, locked,
        ineligible, premature, closed, or superseded targets return None.
        The caller owns this short transaction; this operation never publishes.
        """

    def recover_expired(
        self,
        context: WorkspaceContext,
        *,
        event_id: UUID,
        execution_lease_id: UUID,
        now: datetime,
        maximum_attempts: int,
    ) -> OutboxEvent | None:
        """Prepare a bounded successor only for the same expired canonical execution fence.

        Lock/reload the active/current source, run, and exact latest intent. Preserve all
        execution metadata and previous receipts; never claim work or contact the broker.
        Exhausted execution/generation allowances close mature published intent as BLOCKED
        without changing processing. Unknown, foreign, changed/live/locked, terminal, or
        already closed targets return None.
        The caller supplies its existing processing profile's attempt ceiling.
        """

    def claim_next_due(self, context: WorkspaceContext, *, now: datetime) -> OutboxEvent | None:
        """Inspect at most one unlocked due row after the caller checks broker readiness.

        Only a CLAIMED result authorizes publication. CANCELLED/BLOCKED results count toward
        the caller's scan bound; None means no unlocked due row in this trusted workspace.
        The caller must commit and close this transaction before contacting the broker.
        Expired-run claims require an explicitly configured processing attempt ceiling.
        Otherwise keep unfinished recovery intent deferred; never cancel it as legacy work.
        """

    def record_published(
        self, context: WorkspaceContext, *, event_id: UUID, lease_id: UUID, now: datetime
    ) -> bool:
        """Persist an acknowledgement only for the matching, still-live delivery claim."""

    def record_failure(
        self,
        context: WorkspaceContext,
        *,
        event_id: UUID,
        lease_id: UUID,
        now: datetime,
        category: DeliveryErrorCategory,
        retry_delay_seconds: float,
    ) -> bool:
        """Persist a bounded retry or safe terminal reason under the same claim fence."""


class OutboxDeliveryUnitOfWork(Protocol):
    @property
    def delivery(self) -> OutboxDeliveryRepository:
        """Expose delivery bookkeeping in one short database transaction."""


type OutboxDeliveryUnitOfWorkFactory = Callable[
    [], AbstractContextManager[OutboxDeliveryUnitOfWork]
]
