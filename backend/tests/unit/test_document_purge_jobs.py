"""Verify ID-only publication, trusted execution, and bounded Celery purge retries."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from unittest.mock import create_autospec
from uuid import UUID, uuid4

import pytest
from celery import Celery  # type: ignore[import-untyped]
from celery.exceptions import CeleryError, SoftTimeLimitExceeded  # type: ignore[import-untyped]
from kombu.exceptions import KombuError  # type: ignore[import-untyped]
from pydantic import ValidationError
from redis.exceptions import RedisError

from investment_research.infrastructure.queue.celery import (
    BROKER_VISIBILITY_TIMEOUT_SECONDS,
    PROCESSING_QUEUE_NAME,
    PURGE_DOCUMENT_TASK_NAME,
    CeleryDocumentPurgeDispatcher,
)
from investment_research.infrastructure.queue.deletion_tasks import (
    PURGE_TASK_MAX_BACKOFF_SECONDS,
    PURGE_TASK_MAX_RETRIES,
    PURGE_TASK_SOFT_TIME_LIMIT_SECONDS,
    PURGE_TASK_TIME_LIMIT_SECONDS,
    DocumentPurgeTaskHandler,
    InvalidDocumentPurgePayloadError,
    register_document_purge_tasks,
)
from investment_research.modules.documents.deletion import (
    ExecuteDocumentPurgeJobService,
    PurgeDocumentService,
)
from investment_research.modules.documents.deletion_contracts import (
    DocumentDeletionReceipt,
    DocumentDeletionState,
    DocumentPurgeJobV1,
)
from investment_research.modules.documents.deletion_ports import (
    DocumentDeletionUnavailableError,
    DocumentPurgeDispatchError,
    DocumentPurgePendingError,
)
from investment_research.modules.identity.application import FixedLocalIdentityContextProvider

DOCUMENT_ID = UUID("10000000-0000-4000-8000-000000000001")
CONTEXT = FixedLocalIdentityContextProvider().resolve()
NOW = datetime(2026, 10, 12, tzinfo=UTC)


def make_job() -> DocumentPurgeJobV1:
    return DocumentPurgeJobV1.create(workspace_id=CONTEXT.workspace_id, document_id=DOCUMENT_ID)


@dataclass
class RecordingPurgeExecutor:
    error: Exception | None = None
    jobs: list[DocumentPurgeJobV1] = field(default_factory=list)

    def execute_purge(self, job: DocumentPurgeJobV1) -> DocumentDeletionReceipt:
        self.jobs.append(job)
        if self.error is not None:
            raise self.error
        return DocumentDeletionReceipt(
            workspace_id=job.workspace_id,
            document_id=job.document_id,
            state=DocumentDeletionState.COMPLETED,
            requested_at=NOW,
            completed_at=NOW,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": "document-purge-job.v2"},
        {"document_id": "not-a-uuid"},
        {"idempotency_key": "forged"},
        {"storage_key": "must-not-appear"},
        {"vector_ids": ["must-not-appear"]},
    ],
)
def test_purge_job_rejects_untrusted_fields_and_identity(change: dict[str, object]) -> None:
    payload = make_job().model_dump(mode="json") | change
    with pytest.raises(ValidationError):
        DocumentPurgeJobV1.model_validate(payload)
    executor = RecordingPurgeExecutor()
    with pytest.raises(InvalidDocumentPurgePayloadError) as error:
        DocumentPurgeTaskHandler(executor).handle(payload)
    assert executor.jobs == []
    assert "must-not-appear" not in str(error.value)


def test_job_has_stable_identity_and_contains_only_ids() -> None:
    job = make_job()
    assert job == make_job()
    assert DocumentPurgeJobV1.model_validate_json(job.model_dump_json()) == job
    assert set(job.model_dump()) == {
        "schema_version",
        "workspace_id",
        "document_id",
        "idempotency_key",
    }
    with pytest.raises(ValidationError):
        job.document_id = uuid4()


def test_execution_resolves_trusted_workspace_before_any_cleanup() -> None:
    purge = create_autospec(PurgeDocumentService, instance=True)
    service = ExecuteDocumentPurgeJobService(purge, FixedLocalIdentityContextProvider())
    service.execute(make_job())
    purge.purge.assert_called_once_with(CONTEXT, DOCUMENT_ID)
    purge.purge.reset_mock()
    foreign = DocumentPurgeJobV1.create(workspace_id=uuid4(), document_id=DOCUMENT_ID)
    with pytest.raises(DocumentDeletionUnavailableError, match="unavailable"):
        service.execute(foreign)
    purge.purge.assert_not_called()


def test_dispatcher_publishes_only_validated_job_and_ignores_result() -> None:
    application = cast(Celery, create_autospec(Celery, instance=True))
    CeleryDocumentPurgeDispatcher(application).dispatch(make_job())
    application.send_task.assert_called_once_with(
        PURGE_DOCUMENT_TASK_NAME,
        kwargs={"payload": make_job().model_dump(mode="json")},
        task_id=make_job().idempotency_key,
        queue=PROCESSING_QUEUE_NAME,
        serializer="json",
        ignore_result=True,
    )


@pytest.mark.parametrize("error_type", [CeleryError, KombuError, RedisError, OSError])
def test_dispatch_failure_is_safe_and_does_not_expose_provider_details(
    error_type: type[Exception],
) -> None:
    application = cast(Celery, create_autospec(Celery, instance=True))
    application.send_task.side_effect = error_type("must-not-appear")
    with pytest.raises(DocumentPurgeDispatchError) as error:
        CeleryDocumentPurgeDispatcher(application).dispatch(make_job())
    assert "must-not-appear" not in str(error.value)
    assert error.value.__cause__ is None and error.value.__suppress_context__


def test_unexpected_dispatch_error_is_not_hidden() -> None:
    application = cast(Celery, create_autospec(Celery, instance=True))
    application.send_task.side_effect = TypeError("synthetic programming error")
    with pytest.raises(TypeError):
        CeleryDocumentPurgeDispatcher(application).dispatch(make_job())


def test_handler_converts_soft_timeout_to_safe_pending_failure() -> None:
    executor = RecordingPurgeExecutor(error=SoftTimeLimitExceeded())
    with pytest.raises(DocumentPurgePendingError, match="attempt limit"):
        DocumentPurgeTaskHandler(executor).handle(make_job().model_dump(mode="json"))


def test_registration_executes_duplicates_and_limits_pending_retries() -> None:
    application = Celery("document-purge-task-test", broker="memory://")
    executor = RecordingPurgeExecutor()
    try:
        register_document_purge_tasks(application, executor)
        task = application.tasks[PURGE_DOCUMENT_TASK_NAME]
        payload = make_job().model_dump(mode="json")
        task.run(payload=payload)
        task.run(payload=payload)
        assert executor.jobs == [make_job(), make_job()]
        assert task.ignore_result is True
        assert task.autoretry_for == (DocumentPurgePendingError,)
        assert task.max_retries == PURGE_TASK_MAX_RETRIES
        assert task.retry_backoff is True and task.retry_jitter is True
        assert task.retry_backoff_max == PURGE_TASK_MAX_BACKOFF_SECONDS
        assert task.soft_time_limit == PURGE_TASK_SOFT_TIME_LIMIT_SECONDS
        assert task.time_limit == PURGE_TASK_TIME_LIMIT_SECONDS
        assert task.soft_time_limit < task.time_limit < BROKER_VISIBILITY_TIMEOUT_SECONDS
        application.conf.task_always_eager = True
        executor.jobs.clear()
        executor.error = DocumentPurgePendingError("synthetic pending cleanup")
        result = task.apply(kwargs={"payload": payload}, throw=False)
        assert result.failed()
        assert len(executor.jobs) == PURGE_TASK_MAX_RETRIES + 1
    finally:
        application.close()


@pytest.mark.parametrize("invalid_payload", [False, True])
def test_invalid_or_unavailable_jobs_are_not_retried(invalid_payload: bool) -> None:
    application = Celery("document-purge-invalid-task-test", broker="memory://")
    executor = RecordingPurgeExecutor(error=DocumentDeletionUnavailableError("unavailable"))
    try:
        register_document_purge_tasks(application, executor)
        application.conf.task_always_eager = True
        task = application.tasks[PURGE_DOCUMENT_TASK_NAME]
        result = task.apply(
            kwargs={"payload": {} if invalid_payload else make_job().model_dump(mode="json")},
            throw=False,
        )
        assert result.failed()
        assert len(executor.jobs) == (0 if invalid_payload else 1)
    finally:
        application.close()
