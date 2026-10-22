"""Celery configuration and publication adapter for canonical processing jobs."""

from dataclasses import dataclass
from typing import Final

from celery import Celery  # type: ignore[import-untyped]
from celery.exceptions import CeleryError  # type: ignore[import-untyped]
from kombu import Queue  # type: ignore[import-untyped]
from kombu.exceptions import KombuError  # type: ignore[import-untyped]
from redis.exceptions import RedisError

from investment_research.infrastructure.logging import application_logger
from investment_research.modules.documents.deletion_contracts import DocumentPurgeJobV1
from investment_research.modules.documents.deletion_ports import DocumentPurgeDispatchError
from investment_research.modules.processing.contracts import ProcessingJobV1
from investment_research.modules.processing.ports import ProcessingDispatchError
from investment_research.settings import QueueSettings

CELERY_APPLICATION_NAME: Final = "investment-research"
PROCESSING_QUEUE_NAME: Final = "document-processing"
PROCESS_DOCUMENT_TASK_NAME: Final = "investment_research.processing.process_document.v1"
PURGE_DOCUMENT_TASK_NAME: Final = "investment_research.documents.purge_document.v1"
REPAIR_OUTBOX_TASK_NAME: Final = "investment_research.delivery.repair_outbox.v1"
BROKER_VISIBILITY_TIMEOUT_SECONDS: Final = 420
BROKER_READINESS_TIMEOUT_SECONDS: Final = 2

_logger = application_logger("queue")


def create_celery_app(settings: QueueSettings) -> Celery:
    """Create one broker-only Celery application without opening a connection."""

    application = Celery(
        CELERY_APPLICATION_NAME,
        broker=settings.require_redis_url(),
        backend=None,
    )
    application.conf.update(
        accept_content=("json",),
        broker_connection_retry_on_startup=True,
        broker_transport_options={
            "visibility_timeout": BROKER_VISIBILITY_TIMEOUT_SECONDS,
        },
        enable_utc=True,
        result_backend=None,
        result_backend_transport_options={
            "visibility_timeout": BROKER_VISIBILITY_TIMEOUT_SECONDS,
        },
        result_serializer="json",
        task_acks_late=True,
        task_acks_on_failure_or_timeout=True,
        task_create_missing_queues=False,
        task_default_queue=PROCESSING_QUEUE_NAME,
        task_ignore_result=True,
        task_publish_retry=True,
        task_publish_retry_policy={
            "interval_max": 1.0,
            "interval_start": 0.2,
            "interval_step": 0.2,
            "max_retries": 3,
        },
        task_queues=(Queue(PROCESSING_QUEUE_NAME),),
        task_reject_on_worker_lost=True,
        task_routes={
            PROCESS_DOCUMENT_TASK_NAME: {"queue": PROCESSING_QUEUE_NAME},
            PURGE_DOCUMENT_TASK_NAME: {"queue": PROCESSING_QUEUE_NAME},
            REPAIR_OUTBOX_TASK_NAME: {"queue": PROCESSING_QUEUE_NAME},
        },
        task_serializer="json",
        task_store_errors_even_if_ignored=False,
        timezone="UTC",
        visibility_timeout=BROKER_VISIBILITY_TIMEOUT_SECONDS,
        worker_cancel_long_running_tasks_on_connection_loss=True,
        worker_hijack_root_logger=False,
        worker_prefetch_multiplier=1,
        worker_redirect_stdouts=False,
    )
    return application


@dataclass(frozen=True, slots=True)
class CeleryBrokerReadinessProbe:
    """Check that the configured broker accepts a bounded connection."""

    application: Celery

    def is_ready(self) -> bool:
        """Return false without exposing broker connection details."""

        try:
            with self.application.connection_for_read() as connection:
                connection.ensure_connection(
                    max_retries=0,
                    timeout=BROKER_READINESS_TIMEOUT_SECONDS,
                )
        except (CeleryError, KombuError, RedisError, OSError) as error:
            _logger.warning(
                "health.queue.unavailable",
                extra={"exception_type": type(error).__name__},
            )
            return False
        return True


@dataclass(frozen=True, slots=True)
class CeleryProcessingDispatcher:
    """Publish typed processing jobs without leaking Celery into application code."""

    application: Celery

    def dispatch(self, job: ProcessingJobV1) -> None:
        """Publish one JSON-only task or raise the application-owned queue error."""

        try:
            self.application.send_task(
                PROCESS_DOCUMENT_TASK_NAME,
                kwargs={"payload": job.model_dump(mode="json")},
                task_id=job.idempotency_key,
                queue=PROCESSING_QUEUE_NAME,
                serializer="json",
                ignore_result=True,
            )
        except (CeleryError, KombuError, RedisError, OSError) as error:
            raise ProcessingDispatchError("processing job publication failed") from error


@dataclass(frozen=True, slots=True)
class CeleryDocumentPurgeDispatcher:
    """Publish exact canonical purge identities without document content or SDK errors."""

    application: Celery

    def dispatch(self, job: DocumentPurgeJobV1) -> None:
        try:
            self.application.send_task(
                PURGE_DOCUMENT_TASK_NAME,
                kwargs={"payload": job.model_dump(mode="json")},
                task_id=job.idempotency_key,
                queue=PROCESSING_QUEUE_NAME,
                serializer="json",
                ignore_result=True,
            )
        except CeleryError, KombuError, RedisError, OSError:
            raise DocumentPurgeDispatchError("document purge publication failed") from None
