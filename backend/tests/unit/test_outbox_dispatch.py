"""Targeted delivery shares bounded sweep behavior without claiming unrelated intent."""

from unittest.mock import Mock
from uuid import uuid4

import pytest

from investment_research.modules.delivery.application import (
    DrainOutboxService,
    DrainStopReason,
    OutboxDrainResult,
)
from investment_research.modules.delivery.dispatch import DispatchCapturedProcessingService
from investment_research.modules.delivery.domain import DeliveryErrorCategory, OutboxState
from investment_research.modules.delivery.ports import (
    OutboxPublicationInvalidError,
    OutboxPublicationUnavailableError,
)
from investment_research.modules.processing.contracts import ProcessingJobV1
from investment_research.modules.processing.ports import (
    ProcessingDispatcher,
    ProcessingDispatchError,
)
from tests.unit.test_outbox_contracts import NOW
from tests.unit.test_outbox_drain import Harness


def targeted_harness() -> Harness:
    harness = Harness(count=2)
    harness.repository.claim_due_event.return_value = harness.events[0]
    return harness


def test_exact_delivery_checks_readiness_then_commits_before_one_publication_and_receipt() -> None:
    harness = targeted_harness()
    event = harness.events[0]
    result = harness.service.deliver_event(harness.context, event_id=event.event_id)
    harness.repository.claim_due_event.assert_called_once_with(
        harness.context, event_id=event.event_id, now=NOW
    )
    harness.repository.claim_next_due.assert_not_called()
    assert (result.scanned, result.claimed, result.published, result.rejected_receipts) == (
        1,
        1,
        1,
        0,
    )
    assert result.stop_reason is DrainStopReason.TARGET_LIMIT
    harness.publisher.is_ready.assert_called_once_with(timeout_seconds=2)
    harness.publisher.publish.assert_called_once_with(
        ProcessingJobV1.create(workspace_id=event.workspace_id, processing_run_id=event.target_id),
        timeout_seconds=2,
    )
    harness.repository.record_published.assert_called_once_with(
        harness.context, event_id=event.event_id, lease_id=event.lease_id, now=NOW
    )
    assert harness.active_transactions == 0


@pytest.mark.parametrize("invalid", [False, True])
def test_unavailable_or_invalid_broker_cannot_claim_an_event(invalid: bool) -> None:
    harness = targeted_harness()
    harness.publisher.is_ready.side_effect = (
        OutboxPublicationInvalidError("must-not-appear") if invalid else None
    )
    harness.publisher.is_ready.return_value = False
    result = harness.service.deliver_event(harness.context, event_id=harness.events[0].event_id)
    assert result.claimed == result.scanned == 0
    assert result.stop_reason is (
        DrainStopReason.INVALID_PUBLICATION if invalid else DrainStopReason.BROKER_UNAVAILABLE
    )
    harness.repository.claim_due_event.assert_not_called()
    harness.publisher.publish.assert_not_called()


@pytest.mark.parametrize("state", [None, OutboxState.CANCELLED, OutboxState.BLOCKED])
def test_absent_or_nonpublishable_exact_target_never_falls_back(state: OutboxState | None) -> None:
    harness = targeted_harness()
    event = harness.events[0]
    result_event = None
    if state is OutboxState.CANCELLED:
        result_event = event.cancel()
    elif state is OutboxState.BLOCKED:
        assert event.lease_id is not None
        result_event = event.record_failure(
            category=DeliveryErrorCategory.INVALID_JOB,
            lease_id=event.lease_id,
            completed_at=NOW,
            retry_delay_seconds=0,
        )
    harness.repository.claim_due_event.return_value = result_event
    result = harness.service.deliver_event(harness.context, event_id=event.event_id)
    assert result.published == result.claimed == 0
    harness.repository.claim_due_event.assert_called_once()
    harness.repository.claim_next_due.assert_not_called()
    harness.publisher.publish.assert_not_called()


@pytest.mark.parametrize(
    ("error_type", "reason"),
    [
        (OutboxPublicationUnavailableError, DrainStopReason.BROKER_UNAVAILABLE),
        (OutboxPublicationInvalidError, DrainStopReason.INVALID_PUBLICATION),
    ],
)
def test_uncertain_and_invalid_sends_record_failure_without_retrying(
    error_type: type[Exception], reason: DrainStopReason
) -> None:
    harness = targeted_harness()
    harness.publisher.publish.side_effect = error_type("must-not-appear")
    result = harness.service.deliver_event(harness.context, event_id=harness.events[0].event_id)
    assert result.stop_reason is reason and result.published == 0
    harness.publisher.publish.assert_called_once()
    harness.repository.record_failure.assert_called_once()
    harness.repository.record_published.assert_not_called()
    assert "must-not-appear" not in repr(result)


def test_late_success_is_uncertain_and_receipt_rejection_is_not_success() -> None:
    harness = targeted_harness()

    def late(job: object, *, timeout_seconds: float) -> None:
        harness.elapsed += timeout_seconds

    harness.publisher.publish.side_effect = late
    result = harness.service.deliver_event(harness.context, event_id=harness.events[0].event_id)
    assert result.stop_reason is DrainStopReason.BROKER_UNAVAILABLE
    harness.repository.record_published.assert_not_called()
    harness = targeted_harness()
    harness.repository.record_published.return_value = False
    result = harness.service.deliver_event(harness.context, event_id=harness.events[0].event_id)
    assert result.published == 0 and result.rejected_receipts == 1


@pytest.mark.parametrize("during", ["readiness", "claim"])
def test_shared_deadline_cannot_start_new_publication_after_slow_work(during: str) -> None:
    harness = targeted_harness()

    def slow(*args: object, **kwargs: object) -> object:
        harness.elapsed = 30
        return True if during == "readiness" else harness.events[0]

    if during == "readiness":
        harness.publisher.is_ready.side_effect = slow
    else:
        harness.repository.claim_due_event.side_effect = slow
    result = harness.service.deliver_event(harness.context, event_id=harness.events[0].event_id)
    assert result.stop_reason is DrainStopReason.DEADLINE
    harness.publisher.publish.assert_not_called()


def test_exact_delivery_keeps_reduced_remaining_time_and_propagates_unexpected_failures() -> None:
    harness = targeted_harness()

    def slow_readiness(*, timeout_seconds: float) -> bool:
        harness.elapsed = 29.5
        return True

    harness.publisher.is_ready.side_effect = slow_readiness
    assert (
        harness.service.deliver_event(
            harness.context, event_id=harness.events[0].event_id
        ).published
        == 1
    )
    assert harness.publisher.publish.call_args.kwargs["timeout_seconds"] == 0.5
    harness = targeted_harness()
    harness.repository.record_published.side_effect = RuntimeError(
        "synthetic receipt commit failure"
    )
    with pytest.raises(RuntimeError, match="receipt commit failure"):
        harness.service.deliver_event(harness.context, event_id=harness.events[0].event_id)
    harness.repository.record_failure.assert_not_called()


def test_capture_adapter_validates_scope_and_derives_only_the_initial_event() -> None:
    harness = targeted_harness()
    identity = Mock()
    identity.resolve.return_value = harness.context
    adapter: ProcessingDispatcher = DispatchCapturedProcessingService(harness.service, identity)
    job = ProcessingJobV1.create(
        workspace_id=harness.context.workspace_id, processing_run_id=harness.events[0].target_id
    )
    adapter.dispatch(job)
    harness.repository.claim_due_event.assert_called_once_with(
        harness.context, event_id=harness.events[0].event_id, now=NOW
    )
    identity.resolve.assert_called_once_with()


@pytest.mark.parametrize("forged", [False, True])
def test_capture_adapter_rejects_wrong_scope_or_forged_identity_before_delivery(
    forged: bool,
) -> None:
    harness = targeted_harness()
    identity = Mock()
    identity.resolve.return_value = harness.context
    service = Mock(spec=DrainOutboxService)
    job = ProcessingJobV1.create(workspace_id=uuid4(), processing_run_id=uuid4())
    if forged:
        job = job.model_copy(update={"idempotency_key": "must-not-appear"})
    with pytest.raises(ProcessingDispatchError) as error:
        DispatchCapturedProcessingService(service, identity).dispatch(job)
    assert "must-not-appear" not in str(error.value)
    service.deliver_event.assert_not_called()


@pytest.mark.parametrize(
    "result",
    [
        OutboxDrainResult(0, 0, 0, 0, DrainStopReason.BROKER_UNAVAILABLE),
        OutboxDrainResult(0, 0, 0, 0, DrainStopReason.INVALID_PUBLICATION),
        OutboxDrainResult(1, 1, 0, 0, DrainStopReason.DEADLINE),
        OutboxDrainResult(1, 1, 0, 1, DrainStopReason.TARGET_LIMIT),
        OutboxDrainResult(1, 0, 0, 0, DrainStopReason.TARGET_LIMIT),
    ],
)
def test_capture_adapter_maps_expected_failures_to_existing_safe_capture_error(
    result: OutboxDrainResult,
) -> None:
    service = Mock(spec=DrainOutboxService)
    service.deliver_event.return_value = result
    identity = Mock()
    identity.resolve.return_value = targeted_harness().context
    job = ProcessingJobV1.create(
        workspace_id=identity.resolve.return_value.workspace_id, processing_run_id=uuid4()
    )
    with pytest.raises(ProcessingDispatchError, match="publication did not complete"):
        DispatchCapturedProcessingService(service, identity).dispatch(job)
    service.deliver_event.assert_called_once()


def test_already_owned_or_absent_capture_intent_is_noop_without_new_intent() -> None:
    harness = targeted_harness()
    harness.repository.claim_due_event.return_value = None
    identity = Mock()
    identity.resolve.return_value = harness.context
    DispatchCapturedProcessingService(harness.service, identity).dispatch(
        ProcessingJobV1.create(workspace_id=harness.context.workspace_id, processing_run_id=uuid4())
    )
    harness.repository.claim_due_event.assert_called_once()
    harness.publisher.publish.assert_not_called()
