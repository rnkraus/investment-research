"""Single-row outbox claims and fenced receipts in caller-owned transactions."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from investment_research.modules.delivery.domain import (
    OUTBOX_MAX_ATTEMPTS,
    DeliveryErrorCategory,
    OutboxEvent,
    OutboxState,
    OutboxTransitionError,
)
from investment_research.modules.delivery.persistence import OutboxEventRow
from investment_research.modules.documents.domain import DocumentLifecycleState
from investment_research.modules.documents.persistence import DocumentRow, DocumentVersionRow
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.processing.persistence import ProcessingRunRow


class SqlAlchemyOutboxDeliveryRepository:
    """Never commit, contact the broker, or change processing execution attempts here."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def claim_next_due(self, context: WorkspaceContext, *, now: datetime) -> OutboxEvent | None:
        """Lock at most one due row; cancelled/blocked outcomes also consume one scan slot."""

        now = self._aware_time(now)
        row = self._session.scalar(
            select(OutboxEventRow)
            .where(
                OutboxEventRow.workspace_id == context.workspace_id,
                or_(
                    and_(
                        OutboxEventRow.state.in_([OutboxState.PENDING, OutboxState.RETRY_WAIT]),
                        OutboxEventRow.next_attempt_at <= now,
                    ),
                    and_(
                        OutboxEventRow.state == OutboxState.CLAIMED,
                        OutboxEventRow.lease_expires_at <= now,
                    ),
                ),
            )
            .order_by(
                func.coalesce(OutboxEventRow.next_attempt_at, OutboxEventRow.lease_expires_at),
                OutboxEventRow.event_id,
            )
            .limit(1)
            .with_for_update(skip_locked=True, of=OutboxEventRow)
        )
        if row is None:
            return None
        event = row.to_domain()
        if not self._eligible(context, event, require_captured=True):
            # The legacy fast path may already have started/completed this run. Do not
            # turn its unacknowledged PENDING row into another execution allowance.
            result = event.cancel()
        elif event.attempt_count == OUTBOX_MAX_ATTEMPTS:
            result = event.block_expired_final_claim(now=now)
        else:
            result = event.claim(lease_id=uuid4(), claimed_at=now)
        self._store(row, result)
        return result

    def record_published(
        self, context: WorkspaceContext, *, event_id: UUID, lease_id: UUID, now: datetime
    ) -> bool:
        """Reject expired/foreign fences even if no replacement publisher has claimed yet."""

        return self._finish_claim(context, event_id=event_id, lease_id=lease_id, now=now)

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
        return self._finish_claim(
            context,
            event_id=event_id,
            lease_id=lease_id,
            now=now,
            category=category,
            retry_delay_seconds=retry_delay_seconds,
        )

    def _finish_claim(
        self,
        context: WorkspaceContext,
        *,
        event_id: UUID,
        lease_id: UUID,
        now: datetime,
        category: DeliveryErrorCategory | None = None,
        retry_delay_seconds: float = 0,
    ) -> bool:
        now = self._aware_time(now)
        row = self._session.scalar(
            select(OutboxEventRow)
            .where(
                OutboxEventRow.workspace_id == context.workspace_id,
                OutboxEventRow.event_id == event_id,
                OutboxEventRow.state == OutboxState.CLAIMED,
                OutboxEventRow.lease_id == lease_id,
                OutboxEventRow.lease_expires_at > now,
            )
            .with_for_update(of=OutboxEventRow)
        )
        if row is None:
            return False
        event = row.to_domain()
        # Publication can have started the worker already; CAPTURED is intentionally
        # not required for a receipt. The worker still enforces execution eligibility.
        if not self._eligible(context, event, require_captured=False):
            self._store(row, event.cancel())
            return False
        result = (
            event.record_publication(lease_id=lease_id, completed_at=now)
            if category is None
            else event.record_failure(
                lease_id=lease_id,
                completed_at=now,
                category=category,
                retry_delay_seconds=retry_delay_seconds,
            )
        )
        self._store(row, result)
        return True

    def _eligible(
        self, context: WorkspaceContext, event: OutboxEvent, *, require_captured: bool
    ) -> bool:
        query = (
            select(ProcessingRunRow.id)
            .join(
                DocumentVersionRow,
                and_(
                    DocumentVersionRow.workspace_id == ProcessingRunRow.workspace_id,
                    DocumentVersionRow.id == ProcessingRunRow.document_version_id,
                ),
            )
            .join(
                DocumentRow,
                and_(
                    DocumentRow.workspace_id == DocumentVersionRow.workspace_id,
                    DocumentRow.id == DocumentVersionRow.document_id,
                ),
            )
            .where(
                ProcessingRunRow.workspace_id == context.workspace_id,
                ProcessingRunRow.id == event.target_id,
                DocumentRow.lifecycle_state == DocumentLifecycleState.ACTIVE,
                DocumentRow.current_version_id == DocumentVersionRow.id,
            )
        )
        if require_captured:
            query = query.where(ProcessingRunRow.state == ProcessingState.CAPTURED)
        return self._session.scalar(query) is not None

    def _store(self, row: OutboxEventRow, event: OutboxEvent) -> None:
        for field in OutboxEvent.model_fields:
            setattr(row, field, getattr(event, field))
        self._session.flush()

    @staticmethod
    def _aware_time(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise OutboxTransitionError("delivery time must be timezone-aware")
        return value.astimezone(UTC)
