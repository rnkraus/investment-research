"""Disposable queue-verifier fixture, never a normal archive repair command."""

import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from redis import Redis
from sqlalchemy import select

from investment_research.bootstrap import ApiContainer, build_api_container
from investment_research.infrastructure.database.outbox_unit_of_work import (
    create_outbox_delivery_unit_of_work_factory,
)
from investment_research.infrastructure.database.unit_of_work import (
    create_capture_unit_of_work_factory,
)
from investment_research.infrastructure.queue.celery import PROCESSING_QUEUE_NAME
from investment_research.infrastructure.queue.outbox import BoundedCeleryOutboxPublisher
from investment_research.modules.delivery.application import DrainOutboxService, utc_now
from investment_research.modules.delivery.domain import OUTBOX_UNSTARTED_GRACE_SECONDS, OutboxEvent
from investment_research.modules.delivery.persistence import OutboxEventRow
from investment_research.modules.delivery.repair import RepairOutboxService
from investment_research.modules.documents.application import CaptureDocumentService
from investment_research.modules.documents.contracts import PdfCaptureInput
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.application import M0_0_PROCESSING_PROFILE
from investment_research.modules.processing.contracts import ProcessingJobV1
from investment_research.modules.processing.domain import ProcessingRun
from investment_research.modules.processing.persistence import ProcessingRunRow
from investment_research.settings import load_api_settings
from investment_research.settings_models import QueueSettings

VERIFICATION_FLAG = "INVESTMENT_RESEARCH_QUEUE_RECOVERY_VERIFICATION"
TEST_LEASE_SECONDS = 10
WAIT_SECONDS = 30


class DeferredDispatcher:
    def dispatch(self, job: ProcessingJobV1) -> None:
        """Let only the explicit outbox publisher deliver this synthetic fixture."""


def snapshot(
    container: ApiContainer, context: WorkspaceContext, run_id: UUID
) -> tuple[ProcessingRun, tuple[OutboxEvent, ...]]:
    with container.session_factory() as session:
        run = session.scalars(
            select(ProcessingRunRow).where(
                ProcessingRunRow.workspace_id == context.workspace_id, ProcessingRunRow.id == run_id
            )
        ).one()
        events = session.scalars(
            select(OutboxEventRow)
            .where(
                OutboxEventRow.workspace_id == context.workspace_id,
                OutboxEventRow.target_id == run_id,
            )
            .order_by(OutboxEventRow.generation)
        )
        return run.to_domain(), tuple(row.to_domain() for row in events)


def wait_for_expiry(
    expires_at: datetime,
    *,
    clock: Callable[[], datetime] = utc_now,
    monotonic_clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    deadline = monotonic_clock() + WAIT_SECONDS
    while clock() < expires_at:
        if monotonic_clock() >= deadline:
            raise TimeoutError("synthetic execution lease did not expire before timeout")
        sleep(0.25)


def verify(container: ApiContainer) -> None:
    context = container.identity_context_provider.resolve()
    # Arrange an already mature publication through injected fixture clocks, never row rewrites.
    captured_at = utc_now() - timedelta(seconds=OUTBOX_UNSTARTED_GRACE_SECONDS + 1)
    capture = CaptureDocumentService(
        object_storage=container.object_storage,
        unit_of_work_factory=create_capture_unit_of_work_factory(container.session_factory),
        max_upload_bytes=container.settings.max_upload_bytes,
        processing_dispatcher=DeferredDispatcher(),
        clock=lambda: captured_at,
    )
    with Path("/verification-fixture.pdf").open("rb") as content:
        accepted = capture.capture_pdf(
            context,
            PdfCaptureInput(
                filename="northstar-grid-analytics-fy2025-earnings-brief.pdf",
                media_type="application/pdf",
                title="Expired execution recovery verification fixture",
                language="en",
                published_at=datetime(2026, 2, 20, 12, tzinfo=UTC),
            ),
            content,
        )
    maximum_attempts = M0_0_PROCESSING_PROFILE.maximum_attempts
    factory = create_outbox_delivery_unit_of_work_factory(
        container.session_factory, maximum_processing_attempts=maximum_attempts
    )
    publisher = BoundedCeleryOutboxPublisher(
        QueueSettings(
            runtime_location=container.settings.runtime_location,
            redis_url=container.settings.redis_url,
        )
    )
    first = DrainOutboxService(factory, publisher, clock=lambda: captured_at).drain(context)
    assert first.claimed == first.published == 1
    with container.session_factory.begin() as session:
        row = session.scalars(
            select(ProcessingRunRow)
            .where(
                ProcessingRunRow.workspace_id == context.workspace_id,
                ProcessingRunRow.id == accepted.processing_run_id,
            )
            .with_for_update()
        ).one()
        now = utc_now()
        # Test-only abandoned execution, not an actual second worker crash or production policy.
        row.apply_state(
            row.to_domain().claim_for_processing(
                lease_id=uuid4(),
                claimed_at=now,
                lease_expires_at=now + timedelta(seconds=TEST_LEASE_SECONDS),
                maximum_attempts=maximum_attempts,
            )
        )
    before = snapshot(container, context, accepted.processing_run_id)
    assert before[0].attempt_count == 1 and before[0].lease_expires_at is not None
    repair = RepairOutboxService(factory, publisher, maximum_attempts)
    with Redis.from_url(
        container.settings.require_redis_url(), socket_connect_timeout=2, socket_timeout=2
    ) as broker:
        assert broker.llen(PROCESSING_QUEUE_NAME) == 1
        assert broker.lpop(PROCESSING_QUEUE_NAME) is not None
        live = repair.repair(context)
        assert live.used_targets == live.expired.prepared == live.delivery.published == 0
        assert snapshot(container, context, accepted.processing_run_id) == before
        wait_for_expiry(before[0].lease_expires_at)
        recovered = repair.repair(context)
        assert recovered.expired.prepared == recovered.delivery.published == 1
        assert recovered.unstarted.prepared == 0 and recovered.used_targets == 2
        run, events = snapshot(container, context, accepted.processing_run_id)
        assert run == before[0] and events[0] == before[1][0] and len(events) == 2
        assert events[1].generation == 1 and events[1].attempt_count == 1
        assert broker.llen(PROCESSING_QUEUE_NAME) == 1
        repeated = repair.repair(context)
        assert repeated.used_targets == repeated.delivery.published == 0
        assert snapshot(container, context, accepted.processing_run_id) == (run, events)
    print("Expired execution repaired without resetting its fence, attempts, or prior receipt.")


def main() -> int:
    if os.environ.get(VERIFICATION_FLAG) != "1":
        print("Explicit isolated queue verification is required.", file=sys.stderr)
        return 2
    settings = load_api_settings(env_file=None)
    if (
        settings.environment != "test"
        or settings.runtime_location != "container"
        or settings.processing_profile != "m0-regression"
    ):
        print("The isolated container regression profile is required.", file=sys.stderr)
        return 2
    container = build_api_container(settings)
    try:
        verify(container)
    finally:
        container.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
