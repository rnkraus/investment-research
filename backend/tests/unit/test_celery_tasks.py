"""Unit tests for thin Celery processing-task adapters."""

from dataclasses import dataclass, field
from typing import cast
from uuid import UUID

import pytest
from celery import Celery  # type: ignore[import-untyped]

from investment_research.infrastructure.queue.celery import PROCESS_DOCUMENT_TASK_NAME
from investment_research.infrastructure.queue.tasks import (
    PROCESSING_TASK_MAX_BACKOFF_SECONDS,
    PROCESSING_TASK_MAX_RETRIES,
    InvalidProcessingJobPayloadError,
    ProcessingTaskHandler,
    register_processing_tasks,
)
from investment_research.modules.processing.application import (
    ProcessingRetryableError,
    ProcessingRunAlreadyActiveError,
)
from investment_research.modules.processing.contracts import ProcessingJobV1
from investment_research.modules.processing.domain import ProcessingRun

WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
PROCESSING_RUN_ID = UUID("10000000-0000-4000-8000-000000000004")


@dataclass(slots=True)
class RecordingProcessingJobExecutor:
    """Record validated jobs and optionally simulate one application failure."""

    error: Exception | None = None
    jobs: list[ProcessingJobV1] = field(default_factory=list)

    def execute(self, job: ProcessingJobV1) -> ProcessingRun:
        self.jobs.append(job)
        if self.error is not None:
            raise self.error
        return cast(ProcessingRun, object())


def make_job() -> ProcessingJobV1:
    return ProcessingJobV1.create(
        workspace_id=WORKSPACE_ID,
        processing_run_id=PROCESSING_RUN_ID,
    )


def test_handler_validates_json_before_execution() -> None:
    executor = RecordingProcessingJobExecutor()
    handler = ProcessingTaskHandler(executor)
    job = make_job()

    handler.handle(job.model_dump(mode="json"))

    assert executor.jobs == [job]


def test_handler_rejects_invalid_payload_without_reproducing_input() -> None:
    executor = RecordingProcessingJobExecutor()
    handler = ProcessingTaskHandler(executor)

    with pytest.raises(
        InvalidProcessingJobPayloadError,
        match="processing job payload is invalid",
    ) as error:
        handler.handle(
            {
                **make_job().model_dump(mode="json"),
                "unexpected_secret": "must-not-appear",
            }
        )

    assert "must-not-appear" not in str(error.value)
    assert executor.jobs == []


def test_handler_acknowledges_an_active_duplicate_without_hiding_other_errors() -> None:
    active_duplicate = RecordingProcessingJobExecutor(
        error=ProcessingRunAlreadyActiveError("processing run already has an active attempt")
    )
    ProcessingTaskHandler(active_duplicate).handle(make_job().model_dump(mode="json"))

    unexpected = RecordingProcessingJobExecutor(error=RuntimeError("synthetic failure"))
    with pytest.raises(RuntimeError, match="synthetic failure"):
        ProcessingTaskHandler(unexpected).handle(make_job().model_dump(mode="json"))


def test_handler_propagates_only_explicit_retry_request() -> None:
    retryable = RecordingProcessingJobExecutor(
        error=ProcessingRetryableError("safe transient failure")
    )

    with pytest.raises(ProcessingRetryableError, match="safe transient failure"):
        ProcessingTaskHandler(retryable).handle(make_job().model_dump(mode="json"))


def test_registration_uses_the_publisher_task_name_and_ignores_results() -> None:
    application = Celery("processing-task-test", broker="memory://")
    executor = RecordingProcessingJobExecutor()

    register_processing_tasks(application, executor)
    task = application.tasks[PROCESS_DOCUMENT_TASK_NAME]
    task.run(payload=make_job().model_dump(mode="json"))

    assert task.name == PROCESS_DOCUMENT_TASK_NAME
    assert task.ignore_result is True
    assert task.autoretry_for == (ProcessingRetryableError,)
    assert task.max_retries == PROCESSING_TASK_MAX_RETRIES
    assert task.retry_backoff is True
    assert task.retry_backoff_max == PROCESSING_TASK_MAX_BACKOFF_SECONDS
    assert task.retry_jitter is True
    assert task.throws == (ProcessingRetryableError,)
    assert executor.jobs == [make_job()]

    application.conf.task_always_eager = True
    executor.error = ProcessingRetryableError("safe transient failure")
    executor.jobs.clear()
    retry_result = task.apply(
        kwargs={"payload": make_job().model_dump(mode="json")},
        throw=False,
    )

    assert retry_result.failed()
    assert len(executor.jobs) == 3
