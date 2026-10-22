"""Single-row outbox claims and fenced receipts in caller-owned transactions."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from investment_research.modules.delivery.budget import validate_target_limit
from investment_research.modules.delivery.domain import (
    OUTBOX_MAX_ATTEMPTS,
    OUTBOX_MAX_GENERATIONS,
    OUTBOX_UNSTARTED_GRACE_SECONDS,
    DeliveryErrorCategory,
    OutboxEvent,
    OutboxKind,
    OutboxState,
    OutboxTransitionError,
)
from investment_research.modules.delivery.persistence import OutboxEventRow
from investment_research.modules.delivery.ports import (
    OUTBOX_RECOVERY_MAX_TARGETS,
    ExpiredOutboxCandidate,
)
from investment_research.modules.documents.domain import DocumentLifecycleState
from investment_research.modules.documents.persistence import DocumentRow, DocumentVersionRow
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.domain import ProcessingRun, ProcessingState
from investment_research.modules.processing.persistence import ProcessingRunRow


class SqlAlchemyOutboxDeliveryRepository:
    """Never commit, contact the broker, or change processing execution attempts here."""

    def __init__(self, session: Session, *, maximum_processing_attempts: int | None = None) -> None:
        if maximum_processing_attempts is not None and maximum_processing_attempts < 1:
            raise ValueError("maximum_processing_attempts must be positive")
        self._session = session
        self._maximum_processing_attempts = maximum_processing_attempts

    def list_unstarted_candidates(
        self, context: WorkspaceContext, *, now: datetime, limit: int = OUTBOX_RECOVERY_MAX_TARGETS
    ) -> tuple[UUID, ...]:
        """Read a bounded snapshot; the targeted operation owns all mutation fences."""

        return tuple(
            self._session.scalars(
                self._recovery_candidates(context, now=now, limit=limit).where(
                    ProcessingRunRow.state == ProcessingState.CAPTURED
                )
            )
        )

    def list_expired_candidates(
        self,
        context: WorkspaceContext,
        *,
        now: datetime,
        maximum_attempts: int,
        limit: int = OUTBOX_RECOVERY_MAX_TARGETS,
    ) -> tuple[ExpiredOutboxCandidate, ...]:
        """Observe execution tokens without locking or authorizing their later recovery."""

        if maximum_attempts < 1:
            raise ValueError("maximum_attempts must be positive")
        now = self._aware_time(now)
        rows = self._session.execute(
            self._recovery_candidates(context, now=now, limit=limit)
            .add_columns(ProcessingRunRow.lease_id)
            .where(
                ProcessingRunRow.state.in_(
                    [
                        ProcessingState.PARSING,
                        ProcessingState.LEXICAL_READY,
                        ProcessingState.INDEXING,
                    ]
                ),
                ProcessingRunRow.completed_at.is_(None),
                ProcessingRunRow.lease_id.is_not(None),
                ProcessingRunRow.lease_expires_at <= now,
            )
        )
        candidates = []
        for event_id, lease_id in rows:
            assert lease_id is not None
            candidates.append(ExpiredOutboxCandidate(event_id, lease_id))
        return tuple(candidates)

    def _recovery_candidates(
        self, context: WorkspaceContext, *, now: datetime, limit: int
    ) -> Select[tuple[UUID]]:
        """Apply shared source, history, grace, ordering, and twenty-target bounds in SQL."""

        validate_target_limit(limit)
        now = self._aware_time(now)
        newer = aliased(OutboxEventRow)
        has_newer = (
            select(newer.event_id)
            .where(
                newer.workspace_id == OutboxEventRow.workspace_id,
                newer.kind == OutboxEventRow.kind,
                newer.target_id == OutboxEventRow.target_id,
                newer.generation > OutboxEventRow.generation,
            )
            .exists()
        )
        return (
            select(OutboxEventRow.event_id)
            .join(
                ProcessingRunRow,
                and_(
                    ProcessingRunRow.workspace_id == OutboxEventRow.workspace_id,
                    ProcessingRunRow.id == OutboxEventRow.processing_run_id,
                    ProcessingRunRow.id == OutboxEventRow.target_id,
                ),
            )
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
                    DocumentRow.current_version_id == DocumentVersionRow.id,
                ),
            )
            .where(
                OutboxEventRow.workspace_id == context.workspace_id,
                OutboxEventRow.kind == OutboxKind.PROCESS_DOCUMENT,
                OutboxEventRow.state == OutboxState.PUBLISHED,
                OutboxEventRow.published_at
                <= now - timedelta(seconds=OUTBOX_UNSTARTED_GRACE_SECONDS),
                DocumentRow.lifecycle_state == DocumentLifecycleState.ACTIVE,
                ~has_newer,
            )
            .order_by(OutboxEventRow.published_at, OutboxEventRow.event_id)
            .limit(limit)
        )

    def recover_unstarted(
        self, context: WorkspaceContext, *, event_id: UUID, now: datetime
    ) -> OutboxEvent | None:
        """Retain receipts and serialize recovery with processing, deletion, and promotion."""

        now = self._aware_time(now)
        target = self._lock_recovery_target(context, event_id=event_id)
        if target is None:
            return None
        run, latest, requested_generation = target
        if run.state is not ProcessingState.CAPTURED:
            return None
        if latest.generation == requested_generation + 1:
            return latest
        if latest.event_id != event_id:
            return None
        try:
            if latest.generation == OUTBOX_MAX_GENERATIONS - 1:
                blocked = latest.block_exhausted_recovery(now=now)
                self._store_recovery_block(blocked)
                return blocked
            successor = latest.redeliver_unstarted(now=now)
        except OutboxTransitionError:
            return None
        return self._insert_recovery_successor(context, run_id=run.id, successor=successor)

    def recover_expired(
        self,
        context: WorkspaceContext,
        *,
        event_id: UUID,
        execution_lease_id: UUID,
        now: datetime,
        maximum_attempts: int,
    ) -> OutboxEvent | None:
        """Prepare intent under the same expired execution fence, without editing the run."""

        now = self._aware_time(now)
        if maximum_attempts < 1:
            raise ValueError("maximum_attempts must be positive")
        target = self._lock_recovery_target(context, event_id=event_id)
        if target is None:
            return None
        run, latest, requested_generation = target
        if (
            run.lease_id != execution_lease_id
            or run.state
            not in {
                ProcessingState.PARSING,
                ProcessingState.LEXICAL_READY,
                ProcessingState.INDEXING,
            }
            or run.completed_at is not None
            or run.lease_expires_at is None
            or run.lease_expires_at > now
        ):
            return None
        if latest.generation == requested_generation + 1:
            return latest
        if latest.event_id != event_id:
            return None
        assert run.lease_expires_at is not None
        try:
            if (
                latest.generation == OUTBOX_MAX_GENERATIONS - 1
                or run.attempt_count >= maximum_attempts
            ):
                blocked = latest.block_exhausted_recovery(now=now)
                self._store_recovery_block(blocked)
                return blocked
            if not run.can_reclaim_expired(now=now, maximum_attempts=maximum_attempts):
                return None
            successor = latest.redeliver_expired(
                now=now, execution_lease_expires_at=run.lease_expires_at
            )
        except OutboxTransitionError:
            return None
        return self._insert_recovery_successor(context, run_id=run.id, successor=successor)

    def _store_recovery_block(self, event: OutboxEvent) -> None:
        """The exact latest event is already locked by _lock_recovery_target."""

        row = self._session.get(OutboxEventRow, event.event_id)
        assert row is not None
        self._store(row, event)

    def _lock_recovery_target(
        self, context: WorkspaceContext, *, event_id: UUID
    ) -> tuple[ProcessingRun, OutboxEvent, int] | None:
        """Skip locked work and refresh snapshots before either recovery policy is applied."""

        target = self._session.execute(
            select(OutboxEventRow.target_id, OutboxEventRow.kind, OutboxEventRow.generation).where(
                OutboxEventRow.workspace_id == context.workspace_id,
                OutboxEventRow.event_id == event_id,
            )
        ).one_or_none()
        if target is None:
            return None
        if not self._lock_recovery_source(context, run_id=target.target_id):
            return None
        run = self._session.scalar(
            select(ProcessingRunRow)
            .where(
                ProcessingRunRow.workspace_id == context.workspace_id,
                ProcessingRunRow.id == target.target_id,
            )
            .with_for_update(skip_locked=True, of=ProcessingRunRow)
            .execution_options(populate_existing=True)
        )
        if run is None:
            return None
        latest_id = (
            select(OutboxEventRow.event_id)
            .where(
                OutboxEventRow.workspace_id == context.workspace_id,
                OutboxEventRow.kind == target.kind,
                OutboxEventRow.target_id == target.target_id,
            )
            .order_by(OutboxEventRow.generation.desc())
            .limit(1)
            .correlate(None)
            .scalar_subquery()
        )
        latest = self._session.scalar(
            select(OutboxEventRow)
            .where(
                OutboxEventRow.workspace_id == context.workspace_id,
                OutboxEventRow.event_id == latest_id,
            )
            .with_for_update(skip_locked=True, of=OutboxEventRow)
            .execution_options(populate_existing=True)
        )
        if latest is None:
            return None
        return run.to_domain(), latest.to_domain(), target.generation

    def _lock_recovery_source(self, context: WorkspaceContext, *, run_id: UUID) -> bool:
        document = self._session.scalar(
            select(DocumentRow.id)
            .join(
                DocumentVersionRow,
                and_(
                    DocumentVersionRow.workspace_id == DocumentRow.workspace_id,
                    DocumentVersionRow.document_id == DocumentRow.id,
                    DocumentVersionRow.id == DocumentRow.current_version_id,
                ),
            )
            .join(
                ProcessingRunRow,
                and_(
                    ProcessingRunRow.workspace_id == DocumentVersionRow.workspace_id,
                    ProcessingRunRow.document_version_id == DocumentVersionRow.id,
                ),
            )
            .where(
                DocumentRow.workspace_id == context.workspace_id,
                DocumentRow.lifecycle_state == DocumentLifecycleState.ACTIVE,
                ProcessingRunRow.id == run_id,
            )
            # SHARE fences deletion/promotion but remains compatible with processing's
            # source-write guard. Skip a locked run instead of reversing its lock order.
            .with_for_update(read=True, skip_locked=True, of=DocumentRow)
        )
        return document is not None

    def _insert_recovery_successor(
        self, context: WorkspaceContext, *, run_id: UUID, successor: OutboxEvent
    ) -> OutboxEvent | None:
        row = self._session.scalar(
            insert(OutboxEventRow)
            .values(**successor.model_dump(), processing_run_id=run_id)
            .on_conflict_do_nothing(
                index_elements=["workspace_id", "kind", "target_id", "generation"]
            )
            .returning(OutboxEventRow)
        )
        if row is None:
            row = self._session.scalar(
                select(OutboxEventRow)
                .where(
                    OutboxEventRow.workspace_id == context.workspace_id,
                    OutboxEventRow.event_id == successor.event_id,
                )
                .execution_options(populate_existing=True)
            )
        return None if row is None else row.to_domain()

    def claim_next_due(self, context: WorkspaceContext, *, now: datetime) -> OutboxEvent | None:
        """Lock at most one due row; cancelled/blocked outcomes also consume one scan slot."""

        now = self._aware_time(now)
        unfinished = and_(
            ProcessingRunRow.state.in_(
                [ProcessingState.PARSING, ProcessingState.LEXICAL_READY, ProcessingState.INDEXING]
            ),
            ProcessingRunRow.completed_at.is_(None),
        )
        allowed = or_(OutboxEventRow.generation == 0, ~unfinished)
        if self._maximum_processing_attempts is not None:
            allowed = or_(allowed, ProcessingRunRow.lease_expires_at <= now)
        newer = aliased(OutboxEventRow)
        has_newer = (
            select(newer.event_id)
            .where(
                newer.workspace_id == OutboxEventRow.workspace_id,
                newer.kind == OutboxEventRow.kind,
                newer.target_id == OutboxEventRow.target_id,
                newer.generation > OutboxEventRow.generation,
            )
            .exists()
        )
        candidate = self._session.execute(
            select(OutboxEventRow, ProcessingRunRow)
            .join(
                ProcessingRunRow,
                and_(
                    ProcessingRunRow.workspace_id == OutboxEventRow.workspace_id,
                    ProcessingRunRow.id == OutboxEventRow.processing_run_id,
                    ProcessingRunRow.id == OutboxEventRow.target_id,
                ),
            )
            .where(
                OutboxEventRow.workspace_id == context.workspace_id,
                allowed,
                ~has_newer,
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
            .with_for_update(skip_locked=True, of=[OutboxEventRow, ProcessingRunRow])
            .execution_options(populate_existing=True)
        ).one_or_none()
        if candidate is None:
            return None
        row: OutboxEventRow
        run_row: ProcessingRunRow
        row, run_row = candidate
        event = row.to_domain()
        run = run_row.to_domain()
        if not self._eligible(context, event, require_captured=False):
            result = event.cancel()
        elif run.state is not ProcessingState.CAPTURED and (
            event.generation == 0 or run.completed_at is not None
        ):
            # The legacy fast path may already have started/completed this run. Do not
            # turn its unacknowledged PENDING row into another execution allowance.
            result = event.cancel()
        elif not self._lock_recovery_source(context, run_id=run.id):
            # A source lock or a concurrent lifecycle change is not permission to cancel
            # durable intent. Close this transaction and let a later sweep requalify it.
            return None
        else:
            if run.state is not ProcessingState.CAPTURED:
                maximum_attempts = self._maximum_processing_attempts
                if (
                    maximum_attempts is None
                    or run.lease_expires_at is None
                    or run.lease_expires_at > now
                ):
                    return None
                if not run.can_reclaim_expired(now=now, maximum_attempts=maximum_attempts):
                    result = event.block_exhausted_target(now=now)
                    self._store(row, result)
                    return result
            result = (
                event.block_expired_final_claim(now=now)
                if event.attempt_count == OUTBOX_MAX_ATTEMPTS
                else event.claim(lease_id=uuid4(), claimed_at=now)
            )
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
