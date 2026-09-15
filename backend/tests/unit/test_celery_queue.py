"""Unit tests for broker-only Celery configuration and job publication."""

from typing import cast
from unittest.mock import create_autospec
from uuid import UUID

import pytest
from celery import Celery  # type: ignore[import-untyped]
from celery.exceptions import CeleryError  # type: ignore[import-untyped]
from kombu.exceptions import KombuError  # type: ignore[import-untyped]
from redis.exceptions import RedisError

from investment_research.infrastructure.queue.celery import (
    PROCESS_DOCUMENT_TASK_NAME,
    PROCESSING_QUEUE_NAME,
    CeleryProcessingDispatcher,
    create_celery_app,
)
from investment_research.modules.processing.contracts import ProcessingJobV1
from investment_research.modules.processing.ports import ProcessingDispatchError
from investment_research.settings import QueueSettings

WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
PROCESSING_RUN_ID = UUID("10000000-0000-4000-8000-000000000004")
REDIS_URL = "redis://:synthetic-secret@127.0.0.1:6379/0"


def make_job() -> ProcessingJobV1:
    """Build one deterministic application-owned processing job."""

    return ProcessingJobV1.create(
        workspace_id=WORKSPACE_ID,
        processing_run_id=PROCESSING_RUN_ID,
    )


def make_celery_mock() -> Celery:
    """Build a typed Celery mock without opening a broker connection."""

    return cast(Celery, create_autospec(Celery, instance=True))


def test_celery_app_is_json_only_broker_configuration() -> None:
    settings = QueueSettings.model_validate({"redis_url": REDIS_URL})
    application = create_celery_app(settings)

    assert application.conf.broker_url == REDIS_URL
    assert application.conf.result_backend is None
    assert application.conf.accept_content == ("json",)
    assert application.conf.task_serializer == "json"
    assert application.conf.result_serializer == "json"
    assert application.conf.task_ignore_result is True
    assert application.conf.task_store_errors_even_if_ignored is False
    assert application.conf.task_create_missing_queues is False
    assert application.conf.task_acks_late is False
    assert application.conf.task_reject_on_worker_lost is False
    assert application.conf.enable_utc is True
    assert application.conf.timezone == "UTC"
    assert tuple(queue.name for queue in application.conf.task_queues) == (PROCESSING_QUEUE_NAME,)
    assert application.conf.task_routes == {
        PROCESS_DOCUMENT_TASK_NAME: {"queue": PROCESSING_QUEUE_NAME},
    }


def test_dispatcher_publishes_only_the_validated_json_job() -> None:
    application = make_celery_mock()
    dispatcher = CeleryProcessingDispatcher(application)
    job = make_job()

    dispatcher.dispatch(job)

    application.send_task.assert_called_once_with(
        PROCESS_DOCUMENT_TASK_NAME,
        kwargs={"payload": job.model_dump(mode="json")},
        task_id=job.idempotency_key,
        queue=PROCESSING_QUEUE_NAME,
        serializer="json",
        ignore_result=True,
    )
    published = application.send_task.call_args.kwargs
    assert "content" not in str(published).casefold()
    assert "storage_key" not in str(published)


@pytest.mark.parametrize(
    "provider_error",
    [
        pytest.param(CeleryError("synthetic failure"), id="celery"),
        pytest.param(KombuError("synthetic failure"), id="kombu"),
        pytest.param(RedisError("synthetic failure"), id="redis"),
        pytest.param(OSError("synthetic failure"), id="socket"),
    ],
)
def test_dispatcher_translates_expected_provider_failures(
    provider_error: Exception,
) -> None:
    application = make_celery_mock()
    application.send_task.side_effect = provider_error
    dispatcher = CeleryProcessingDispatcher(application)

    with pytest.raises(
        ProcessingDispatchError,
        match="processing job publication failed",
    ) as error:
        dispatcher.dispatch(make_job())

    assert error.value.__cause__ is provider_error
    assert "synthetic failure" not in str(error.value)


def test_dispatcher_does_not_hide_programming_errors() -> None:
    application = make_celery_mock()
    application.send_task.side_effect = TypeError("synthetic programming error")
    dispatcher = CeleryProcessingDispatcher(application)

    with pytest.raises(TypeError, match="synthetic programming error"):
        dispatcher.dispatch(make_job())
