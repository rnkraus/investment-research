"""Application-owned outbox persistence within caller-owned transactions."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from investment_research.modules.delivery.domain import DeliveryErrorCategory, OutboxEvent
from investment_research.modules.identity.domain import WorkspaceContext


class OutboxScopeError(ValueError):
    """Reject an unavailable or incompatible target without exposing identifiers."""


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

    def claim_next_due(self, context: WorkspaceContext, *, now: datetime) -> OutboxEvent | None:
        """Inspect at most one unlocked due row after the caller checks broker readiness.

        Only a CLAIMED result authorizes publication. CANCELLED/BLOCKED results count toward
        the caller's scan bound; None means no unlocked due row in this trusted workspace.
        The caller must commit and close this transaction before contacting the broker.
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
