"""One broker-only Beat schedule, bounded publication, safe failures, and separate startup."""

import logging
import signal
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from celery import Celery  # type: ignore[import-untyped]
from celery.beat import ScheduleEntry  # type: ignore[import-untyped]
from pydantic import SecretStr

from investment_research import scheduler as entrypoint
from investment_research.infrastructure.queue import scheduler as scheduler_module
from investment_research.infrastructure.queue.celery import REPAIR_OUTBOX_TASK_NAME
from investment_research.infrastructure.queue.scheduler import (
    REPAIR_SCHEDULE_NAME,
    SCHEDULE_FILENAME,
    OutboxRepairScheduler,
    create_scheduler_application,
)
from investment_research.modules.delivery.contracts import (
    OUTBOX_REPAIR_INTERVAL_SECONDS,
    OutboxRepairTickV1,
)
from investment_research.modules.delivery.ports import (
    OutboxPublicationInvalidError,
    OutboxPublicationUnavailableError,
)
from investment_research.settings import QueueSettings


def settings() -> QueueSettings:
    return QueueSettings(redis_url=SecretStr("redis://:synthetic-scheduler@127.0.0.1:6379/0"))


@pytest.fixture(autouse=True)
def capture_scheduler_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    logger = logging.getLogger("investment_research.delivery.scheduler")
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "level", logging.INFO)


@pytest.fixture
def application() -> Iterator[Celery]:
    app = create_scheduler_application(settings())
    try:
        yield app
    finally:
        app.close()


@pytest.fixture
def beat(application: Celery, tmp_path: Path) -> Iterator[OutboxRepairScheduler]:
    instance = OutboxRepairScheduler(app=application, schedule_filename=str(tmp_path / "schedule"))
    try:
        yield instance
    finally:
        instance.close()


def test_application_has_one_content_free_schedule_and_no_worker_resources(
    application: Celery,
) -> None:
    assert application.conf.beat_schedule == {
        REPAIR_SCHEDULE_NAME: {
            "task": REPAIR_OUTBOX_TASK_NAME,
            "schedule": 30,
            "kwargs": {"payload": {"schema_version": "outbox-repair-tick.v1"}},
        }
    }
    assert OUTBOX_REPAIR_INTERVAL_SECONDS == 30
    assert application.conf.beat_schedule_filename == SCHEDULE_FILENAME
    assert SCHEDULE_FILENAME.startswith("/tmp/")
    assert application.conf.beat_max_loop_interval == 30
    assert application.conf.task_publish_retry is False
    assert application.conf.result_backend is None
    assert application.conf.result_expires is None
    assert application.conf.imports == ()
    assert application.conf.include == ()


def test_real_beat_tick_uses_only_bounded_publisher_and_does_not_resend_immediately(
    monkeypatch: pytest.MonkeyPatch, application: Celery, beat: OutboxRepairScheduler
) -> None:
    publisher = Mock()
    factory = Mock(return_value=publisher)
    monkeypatch.setattr(scheduler_module, "BoundedCeleryOutboxPublisher", factory)
    monkeypatch.setattr(application, "connection_for_write", Mock(side_effect=AssertionError))
    monkeypatch.setattr(application, "send_task", Mock(side_effect=AssertionError))
    assert set(beat.schedule) == {REPAIR_SCHEDULE_NAME}
    assert beat.tick() > 0
    publisher.publish_repair_tick.assert_not_called()
    entry = beat.schedule[REPAIR_SCHEDULE_NAME]
    entry.last_run_at -= timedelta(seconds=31)
    beat._heap = None
    assert beat.tick() == 0
    publisher.publish_repair_tick.assert_called_once_with(timeout_seconds=2)
    broker_settings = factory.call_args.args[0]
    assert broker_settings.database_url is None
    assert broker_settings.require_redis_url() == settings().require_redis_url()
    assert beat.tick() > 0
    publisher.publish_repair_tick.assert_called_once()


@pytest.mark.parametrize(
    ("error_type", "event"),
    [
        (OutboxPublicationUnavailableError, "delivery.repair_tick.unavailable"),
        (OutboxPublicationInvalidError, "delivery.repair_tick.invalid"),
    ],
)
def test_broker_failure_waits_for_later_tick_without_logging_raw_details(
    monkeypatch: pytest.MonkeyPatch,
    beat: OutboxRepairScheduler,
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
    event: str,
) -> None:
    publisher = Mock()
    publisher.publish_repair_tick.side_effect = error_type("must-not-appear connection detail")
    monkeypatch.setattr(
        scheduler_module, "BoundedCeleryOutboxPublisher", Mock(return_value=publisher)
    )
    beat.schedule[REPAIR_SCHEDULE_NAME].last_run_at -= timedelta(seconds=31)
    assert beat.tick() == 0
    assert beat.tick() > 0
    publisher.publish_repair_tick.assert_called_once()
    assert event in caplog.text
    assert "must-not-appear" not in caplog.text


def test_unexpected_publisher_failure_is_not_retried_or_echoed(
    monkeypatch: pytest.MonkeyPatch, beat: OutboxRepairScheduler
) -> None:
    publisher = Mock()
    publisher.publish_repair_tick.side_effect = TypeError("must-not-appear")
    monkeypatch.setattr(
        scheduler_module, "BoundedCeleryOutboxPublisher", Mock(return_value=publisher)
    )
    beat.schedule[REPAIR_SCHEDULE_NAME].last_run_at -= timedelta(seconds=31)
    with pytest.raises(RuntimeError, match=r"^repair tick scheduling failed$") as error:
        beat.tick()
    assert error.value.__cause__ is None
    publisher.publish_repair_tick.assert_called_once()


@pytest.mark.parametrize(
    "override",
    [
        {"task": "unapproved-task"},
        {"args": ("must-not-appear",)},
        {"kwargs": {"payload": {"schema_version": "outbox-repair-tick.v1", "workspace_id": "x"}}},
        {"options": {"queue": "unapproved-queue"}},
    ],
)
def test_unapproved_schedule_entry_cannot_select_another_task_scope_or_options(
    monkeypatch: pytest.MonkeyPatch,
    application: Celery,
    beat: OutboxRepairScheduler,
    caplog: pytest.LogCaptureFixture,
    override: dict[str, object],
) -> None:
    factory = Mock()
    monkeypatch.setattr(scheduler_module, "BoundedCeleryOutboxPublisher", factory)
    entry = ScheduleEntry(
        **{
            "name": REPAIR_SCHEDULE_NAME,
            "task": REPAIR_OUTBOX_TASK_NAME,
            "schedule": 30,
            "kwargs": {"payload": OutboxRepairTickV1.create().model_dump(mode="json")},
            "app": application,
        }
        | override
    )
    beat.apply_entry(entry)
    factory.assert_not_called()
    assert "delivery.repair_tick.entry_rejected" in caplog.text
    assert "must-not-appear" not in caplog.text


def test_entrypoint_builds_separate_service_and_restores_signal_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Mock()
    app.__enter__ = Mock(return_value=app)
    app.__exit__ = Mock(return_value=False)
    constructor = Mock(return_value=app)
    service_factory = Mock()
    previous = object()
    handler = Mock(return_value=previous)
    monkeypatch.setattr(entrypoint, "load_scheduler_settings", settings)
    monkeypatch.setattr(entrypoint, "configure_logging", Mock())
    monkeypatch.setattr(entrypoint, "create_scheduler_application", constructor)
    monkeypatch.setattr(entrypoint, "Service", service_factory)
    monkeypatch.setattr(signal, "signal", handler)
    assert entrypoint.main([]) == 0
    service_factory.assert_called_once_with(app, scheduler_cls=OutboxRepairScheduler)
    service_factory.return_value.start.assert_called_once_with()
    assert handler.call_args_list[0].args == (signal.SIGTERM, entrypoint.stop_scheduler)
    assert handler.call_args_list[1].args == (signal.SIGTERM, previous)
    app.__exit__.assert_called_once()


def test_entrypoint_failure_is_sanitized_and_application_is_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app = Mock()
    app.__enter__ = Mock(return_value=app)
    app.__exit__ = Mock(return_value=False)
    service = Mock()
    service.start.side_effect = RuntimeError("must-not-appear")
    monkeypatch.setattr(entrypoint, "load_scheduler_settings", settings)
    monkeypatch.setattr(entrypoint, "configure_logging", Mock())
    monkeypatch.setattr(entrypoint, "create_scheduler_application", Mock(return_value=app))
    monkeypatch.setattr(entrypoint, "Service", Mock(return_value=service))
    monkeypatch.setattr(signal, "signal", Mock())
    assert entrypoint.main([]) == 1
    assert (
        capsys.readouterr().err == "Outbox scheduler failed at stage=scheduler; details withheld.\n"
    )
    app.__exit__.assert_called_once()


def test_help_does_not_load_configuration_and_configuration_failure_is_safe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    loader = Mock(side_effect=ValueError("must-not-appear"))
    monkeypatch.setattr(entrypoint, "load_scheduler_settings", loader)
    with pytest.raises(SystemExit) as error:
        entrypoint.main(["--help"])
    assert error.value.code == 0
    loader.assert_not_called()
    capsys.readouterr()
    assert entrypoint.main([]) == 1
    assert capsys.readouterr().err == (
        "Outbox scheduler failed at stage=configuration; details withheld.\n"
    )


def test_sigterm_uses_beats_clean_shutdown_path() -> None:
    with pytest.raises(SystemExit) as error:
        entrypoint.stop_scheduler(signal.SIGTERM, None)
    assert error.value.code == 0
