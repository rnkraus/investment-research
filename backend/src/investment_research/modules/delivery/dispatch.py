"""Opt-in adapter for immediate delivery of already committed capture intent."""

from dataclasses import dataclass

from pydantic import ValidationError

from investment_research.modules.delivery.application import DrainOutboxService, DrainStopReason
from investment_research.modules.delivery.domain import OutboxKind, outbox_event_id
from investment_research.modules.identity.application import IdentityContextProvider
from investment_research.modules.processing.contracts import ProcessingJobV1
from investment_research.modules.processing.ports import ProcessingDispatchError


@dataclass(frozen=True, slots=True)
class DispatchCapturedProcessingService:
    """Reuse generation-zero intent; a job payload cannot grant workspace authority."""

    delivery_service: DrainOutboxService
    identity_context_provider: IdentityContextProvider

    def dispatch(self, job: ProcessingJobV1) -> None:
        try:
            job = ProcessingJobV1.model_validate(job.model_dump())
        except ValidationError:
            raise ProcessingDispatchError("captured processing job is invalid") from None
        context = self.identity_context_provider.resolve()
        if context.workspace_id != job.workspace_id:
            raise ProcessingDispatchError("captured processing job is unavailable")

        result = self.delivery_service.deliver_event(
            context,
            event_id=outbox_event_id(
                context.workspace_id, OutboxKind.PROCESS_DOCUMENT, job.processing_run_id, 0
            ),
        )
        if (
            result.stop_reason not in {DrainStopReason.EMPTY, DrainStopReason.TARGET_LIMIT}
            or result.rejected_receipts > 0
            or (result.scanned > 0 and result.published != 1)
        ):
            raise ProcessingDispatchError("captured processing publication did not complete")
        # EMPTY is an idempotent no-op, not proof of publication or processing completion.
        # Another owner, an existing receipt, exclusion, or unknown intent can all leave no claim.
        # Do not create intent, retry directly, or fall back to a later generation here.
