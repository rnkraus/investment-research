"""Thin Celery task adapters for application-owned processing jobs."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from math import ceil

from celery import Celery, Task  # type: ignore[import-untyped]
from pydantic import ValidationError

from investment_research.infrastructure.queue.celery import PROCESS_DOCUMENT_TASK_NAME
from investment_research.modules.processing.application import (
    M0_0_PROCESSING_PROFILE,
    ProcessingRetryableError,
    ProcessingRunAlreadyActiveError,
)
from investment_research.modules.processing.contracts import ProcessingJobV1
from investment_research.modules.processing.ports import ProcessingJobExecutor


class InvalidProcessingJobPayloadError(ValueError):
    """Report an invalid queue payload without reproducing untrusted values."""


PROCESSING_TASK_MAX_RETRIES = M0_0_PROCESSING_PROFILE.maximum_attempts - 1
PROCESSING_TASK_MAX_BACKOFF_SECONDS = 30
MINIMUM_REDELIVERY_DELAY_SECONDS = 1

type TaskClock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class ProcessingTaskHandler:
    """Validate broker input before invoking the application-owned job executor."""

    executor: ProcessingJobExecutor

    def handle(self, payload: object) -> None:
        """Validate and execute one broker-delivered processing job."""

        try:
            job = ProcessingJobV1.model_validate(payload)
        except ValidationError:
            raise InvalidProcessingJobPayloadError("processing job payload is invalid") from None

        self.executor.execute(job)


def redelivery_delay_seconds(*, retry_at: datetime, now: datetime) -> int:
    """Return a positive whole-second delay until the canonical lease expires."""

    if retry_at.tzinfo is None or retry_at.utcoffset() is None:
        raise ValueError("retry_at must be timezone-aware")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return max(MINIMUM_REDELIVERY_DELAY_SECONDS, ceil((retry_at - now).total_seconds()))


def request_was_redelivered(delivery_info: object) -> bool:
    """Read Celery's broker redelivery marker without trusting request metadata."""

    return isinstance(delivery_info, Mapping) and delivery_info.get("redelivered") is True


def register_processing_tasks(
    application: Celery,
    executor: ProcessingJobExecutor,
    *,
    clock: TaskClock = lambda: datetime.now(UTC),
) -> None:
    """Register the versioned processing task on one worker application."""

    handler = ProcessingTaskHandler(executor)

    @application.task(  # type: ignore[untyped-decorator]
        autoretry_for=(ProcessingRetryableError,),
        bind=True,
        name=PROCESS_DOCUMENT_TASK_NAME,
        ignore_result=True,
        max_retries=PROCESSING_TASK_MAX_RETRIES,
        retry_backoff=True,
        retry_backoff_max=PROCESSING_TASK_MAX_BACKOFF_SECONDS,
        retry_jitter=True,
        throws=(ProcessingRetryableError,),
        typing=True,
    )
    def process_document_task(task: Task, *, payload: object) -> None:
        try:
            handler.handle(payload)
        except ProcessingRunAlreadyActiveError as error:
            if not request_was_redelivered(task.request.delivery_info):
                return
            delay = redelivery_delay_seconds(retry_at=error.retry_at, now=clock())
            raise task.retry(exc=error, countdown=delay, throw=False) from error
