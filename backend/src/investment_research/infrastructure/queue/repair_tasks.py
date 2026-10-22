"""Validate internal wake-up signals before invoking bounded canonical outbox repair."""

from dataclasses import dataclass

from celery import Celery  # type: ignore[import-untyped]
from pydantic import ValidationError

from investment_research.infrastructure.queue.celery import REPAIR_OUTBOX_TASK_NAME
from investment_research.modules.delivery.contracts import OutboxRepairTickV1
from investment_research.modules.delivery.ports import OutboxRepairJobExecutor


class InvalidOutboxRepairTickError(ValueError):
    """Reject malformed broker input without echoing its contents."""


@dataclass(frozen=True, slots=True)
class OutboxRepairTaskHandler:
    executor: OutboxRepairJobExecutor

    def handle(self, payload: object) -> None:
        try:
            OutboxRepairTickV1.model_validate(payload)
        except ValidationError:
            raise InvalidOutboxRepairTickError("outbox repair tick is invalid") from None
        self.executor.execute_repair()


def register_outbox_repair_task(application: Celery, executor: OutboxRepairJobExecutor) -> None:
    """Register only; no schedule, sender, eager execution, or automatic task retries."""

    handler = OutboxRepairTaskHandler(executor)

    @application.task(  # type: ignore[untyped-decorator]
        shared=False,
        name=REPAIR_OUTBOX_TASK_NAME,
        ignore_result=True,
        autoretry_for=(),
        max_retries=0,
        throws=(InvalidOutboxRepairTickError,),
        typing=True,
    )
    def repair_outbox_task(*, payload: object) -> None:
        handler.handle(payload)
