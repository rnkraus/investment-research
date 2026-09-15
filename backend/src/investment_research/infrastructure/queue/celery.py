"""Celery configuration and publication adapter for canonical processing jobs."""

from dataclasses import dataclass
from typing import Final

from celery import Celery  # type: ignore[import-untyped]
from celery.exceptions import CeleryError  # type: ignore[import-untyped]
from kombu import Queue  # type: ignore[import-untyped]
from kombu.exceptions import KombuError  # type: ignore[import-untyped]
from redis.exceptions import RedisError

from investment_research.modules.processing.contracts import ProcessingJobV1
from investment_research.modules.processing.ports import ProcessingDispatchError
from investment_research.settings import QueueSettings

CELERY_APPLICATION_NAME: Final = "investment-research"
PROCESSING_QUEUE_NAME: Final = "document-processing"
PROCESS_DOCUMENT_TASK_NAME: Final = "investment_research.processing.process_document.v1"


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
        enable_utc=True,
        result_backend=None,
        result_serializer="json",
        task_acks_late=False,
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
        task_reject_on_worker_lost=False,
        task_routes={
            PROCESS_DOCUMENT_TASK_NAME: {"queue": PROCESSING_QUEUE_NAME},
        },
        task_serializer="json",
        task_store_errors_even_if_ignored=False,
        timezone="UTC",
        worker_hijack_root_logger=False,
        worker_redirect_stdouts=False,
    )
    return application


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
