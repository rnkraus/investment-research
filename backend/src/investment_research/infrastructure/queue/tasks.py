"""Thin Celery task adapters for application-owned processing jobs."""

from dataclasses import dataclass

from celery import Celery  # type: ignore[import-untyped]
from pydantic import ValidationError

from investment_research.infrastructure.queue.celery import PROCESS_DOCUMENT_TASK_NAME
from investment_research.modules.processing.application import ProcessingRunAlreadyActiveError
from investment_research.modules.processing.contracts import ProcessingJobV1
from investment_research.modules.processing.ports import ProcessingJobExecutor


class InvalidProcessingJobPayloadError(ValueError):
    """Report an invalid queue payload without reproducing untrusted values."""


@dataclass(frozen=True, slots=True)
class ProcessingTaskHandler:
    """Validate broker input before invoking the application-owned job executor."""

    executor: ProcessingJobExecutor

    def handle(self, payload: object) -> None:
        """Execute one valid job and acknowledge harmless concurrent redelivery."""

        try:
            job = ProcessingJobV1.model_validate(payload)
        except ValidationError:
            raise InvalidProcessingJobPayloadError("processing job payload is invalid") from None

        try:
            self.executor.execute(job)
        except ProcessingRunAlreadyActiveError:
            return


def register_processing_tasks(
    application: Celery,
    executor: ProcessingJobExecutor,
) -> None:
    """Register the versioned processing task on one worker application."""

    handler = ProcessingTaskHandler(executor)

    @application.task(  # type: ignore[untyped-decorator]
        name=PROCESS_DOCUMENT_TASK_NAME,
        ignore_result=True,
        typing=True,
    )
    def process_document_task(*, payload: object) -> None:
        handler.handle(payload)
