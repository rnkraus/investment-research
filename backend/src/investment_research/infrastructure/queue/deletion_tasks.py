"""Thin, bounded Celery adapters for canonical permanent-document purge jobs."""

from dataclasses import dataclass

from celery import Celery  # type: ignore[import-untyped]
from celery.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]
from pydantic import ValidationError

from investment_research.infrastructure.queue.celery import PURGE_DOCUMENT_TASK_NAME
from investment_research.modules.documents.deletion_contracts import DocumentPurgeJobV1
from investment_research.modules.documents.deletion_ports import (
    DocumentDeletionUnavailableError,
    DocumentPurgeJobExecutor,
    DocumentPurgePendingError,
)

PURGE_TASK_MAX_RETRIES = 4
PURGE_TASK_MAX_BACKOFF_SECONDS = 30
PURGE_TASK_SOFT_TIME_LIMIT_SECONDS = 300
PURGE_TASK_TIME_LIMIT_SECONDS = 330


class InvalidDocumentPurgePayloadError(ValueError):
    """Reject untrusted broker input without echoing its values into task logs."""


@dataclass(frozen=True, slots=True)
class DocumentPurgeTaskHandler:
    executor: DocumentPurgeJobExecutor

    def handle(self, payload: object) -> None:
        try:
            job = DocumentPurgeJobV1.model_validate(payload)
        except ValidationError:
            raise InvalidDocumentPurgePayloadError("document purge payload is invalid") from None
        try:
            self.executor.execute_purge(job)
        except SoftTimeLimitExceeded:
            raise DocumentPurgePendingError("document cleanup exceeded its attempt limit") from None


def register_document_purge_tasks(application: Celery, executor: DocumentPurgeJobExecutor) -> None:
    """Retry pending cleanup, not malformed jobs or unauthorized workspace identities."""

    handler = DocumentPurgeTaskHandler(executor)

    @application.task(  # type: ignore[untyped-decorator]
        shared=False,
        name=PURGE_DOCUMENT_TASK_NAME,
        ignore_result=True,
        autoretry_for=(DocumentPurgePendingError,),
        max_retries=PURGE_TASK_MAX_RETRIES,
        retry_backoff=True,
        retry_backoff_max=PURGE_TASK_MAX_BACKOFF_SECONDS,
        retry_jitter=True,
        soft_time_limit=PURGE_TASK_SOFT_TIME_LIMIT_SECONDS,
        time_limit=PURGE_TASK_TIME_LIMIT_SECONDS,
        throws=(
            DocumentPurgePendingError,
            DocumentDeletionUnavailableError,
            InvalidDocumentPurgePayloadError,
        ),
        typing=True,
    )
    def purge_document_task(*, payload: object) -> None:
        handler.handle(payload)
