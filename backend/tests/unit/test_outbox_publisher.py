"""Safe subprocess transport, one deadline, no hidden publication retries."""

import json
import subprocess
import sys
from io import BytesIO
from time import monotonic
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError
from redis.exceptions import AuthenticationError

from investment_research.infrastructure.queue import outbox, outbox_transport
from investment_research.infrastructure.queue.celery import REPAIR_OUTBOX_TASK_NAME
from investment_research.infrastructure.queue.outbox import BoundedCeleryOutboxPublisher
from investment_research.infrastructure.queue.outbox_transport import BrokerOperation
from investment_research.modules.delivery.ports import (
    OutboxPublicationInvalidError,
    OutboxPublicationUnavailableError,
)
from investment_research.modules.processing.contracts import ProcessingJobV1
from investment_research.settings_models import QueueSettings

SYNTHETIC_BROKER_URL = "redis://:synthetic-broker-only@127.0.0.1:1/0"


def make_publisher() -> BoundedCeleryOutboxPublisher:
    return BoundedCeleryOutboxPublisher(QueueSettings(redis_url=SecretStr(SYNTHETIC_BROKER_URL)))


def make_job() -> ProcessingJobV1:
    return ProcessingJobV1.create(workspace_id=uuid4(), processing_run_id=uuid4())


def install_process_mock(
    monkeypatch: pytest.MonkeyPatch, *, returncode: int = 0
) -> tuple[Mock, Mock]:
    process = Mock()
    process.returncode = returncode
    process.poll.return_value = returncode
    constructor = Mock(return_value=process)
    monkeypatch.setattr(subprocess, "Popen", constructor)
    return constructor, process


def test_credentials_only_travel_through_the_private_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    constructor, process = install_process_mock(monkeypatch)
    monkeypatch.setenv("INVESTMENT_RESEARCH_DATABASE_URL", "synthetic-not-for-child")
    monkeypatch.setenv("INVESTMENT_RESEARCH_OPENAI_GENERATION_API_KEY", "synthetic-not-for-child")
    job = make_job()
    make_publisher().publish(job, timeout_seconds=2)
    assert SYNTHETIC_BROKER_URL not in repr(constructor.call_args)
    arguments = constructor.call_args.kwargs
    assert arguments["stdin"] is subprocess.PIPE
    assert arguments["stdout"] is subprocess.DEVNULL
    assert arguments["stderr"] is subprocess.DEVNULL
    assert set(arguments["env"]) == {
        "PATH",
        "PYTHONPATH",
        "PYTHONNOUSERSITE",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
    }
    payload = json.loads(process.communicate.call_args_list[0].kwargs["input"])
    assert payload["broker_url"] == SYNTHETIC_BROKER_URL
    assert payload["job"] == job.model_dump(mode="json")
    assert 0 < process.communicate.call_args_list[0].kwargs["timeout"] <= 2


@pytest.mark.parametrize("code", [1, 2, 3])
def test_helper_errors_are_safe_and_do_not_hide_programming_faults(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    install_process_mock(monkeypatch, returncode=code)
    expected = {
        1: OutboxPublicationUnavailableError,
        2: OutboxPublicationInvalidError,
        3: RuntimeError,
    }[code]
    with pytest.raises(expected) as captured:
        make_publisher().publish(make_job(), timeout_seconds=2)
    assert "synthetic-broker" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_startup_consumes_the_same_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    constructor, process = install_process_mock(monkeypatch)
    process.poll.return_value = None
    monkeypatch.setattr(outbox, "monotonic", Mock(side_effect=[10, 12]))
    with pytest.raises(OutboxPublicationUnavailableError):
        make_publisher().publish(make_job(), timeout_seconds=2)
    process.kill.assert_called_once()
    # No input/publication was attempted after process creation exhausted the deadline.
    process.communicate.assert_called_once_with()
    constructor.assert_called_once()


def test_timeout_kills_and_reaps_without_exposing_the_private_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, process = install_process_mock(monkeypatch)
    process.poll.return_value = None
    process.communicate.side_effect = [
        subprocess.TimeoutExpired("synthetic-helper", 2),
        (None, None),
    ]
    assert not make_publisher().is_ready(timeout_seconds=2)
    process.kill.assert_called_once()
    assert process.communicate.call_count == 2


def test_late_child_success_is_not_reported_as_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    install_process_mock(monkeypatch)
    monkeypatch.setattr(outbox, "monotonic", Mock(side_effect=[10, 10.1, 12]))
    assert not make_publisher().is_ready(timeout_seconds=2)


@pytest.mark.parametrize("timeout", [0, -1, 2.1, float("nan"), float("inf")])
def test_invalid_deadlines_never_start_a_process(
    monkeypatch: pytest.MonkeyPatch, timeout: float
) -> None:
    constructor, _ = install_process_mock(monkeypatch)
    with pytest.raises(OutboxPublicationInvalidError):
        make_publisher().publish(make_job(), timeout_seconds=timeout)
    constructor.assert_not_called()


def test_missing_configuration_and_invalid_job_never_start_a_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor, _ = install_process_mock(monkeypatch)
    with pytest.raises(OutboxPublicationInvalidError):
        BoundedCeleryOutboxPublisher(QueueSettings()).is_ready(timeout_seconds=2)
    job = make_job().model_copy(update={"idempotency_key": "forged"})
    with pytest.raises(OutboxPublicationInvalidError):
        make_publisher().publish(job, timeout_seconds=2)
    constructor.assert_not_called()


def test_real_slow_helper_is_terminated(monkeypatch: pytest.MonkeyPatch) -> None:
    real_popen = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def slow_helper(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = real_popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        children.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", slow_helper)
    started = monotonic()
    assert not make_publisher().is_ready(timeout_seconds=0.05)
    assert monotonic() - started < 2
    assert children and children[0].poll() is not None


def test_transport_uses_one_connection_and_disables_celery_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = Mock()
    application.__enter__ = Mock(return_value=application)
    application.__exit__ = Mock(return_value=False)
    connection = Mock()
    application.connection_for_write.return_value.__enter__ = Mock(return_value=connection)
    application.connection_for_write.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(outbox_transport, "create_celery_app", Mock(return_value=application))
    job = make_job()
    request = BrokerOperation(
        operation="publish",
        broker_url=SecretStr(SYNTHETIC_BROKER_URL),
        runtime_location="host",
        deadline=monotonic() + 2,
        job=job,
    )
    assert outbox_transport.execute_operation(request) == 0
    application.send_task.assert_called_once()
    arguments = application.send_task.call_args.kwargs
    assert arguments["connection"] is connection
    assert arguments["retry"] is False
    assert arguments["retry_policy"] == {"max_retries": 0}
    assert arguments["kwargs"] == {"payload": job.model_dump(mode="json")}
    assert connection.ensure_connection.call_args.kwargs["max_retries"] == 0


def test_transport_rejects_probe_with_a_job_and_unknown_fields() -> None:
    for changes in ({"operation": "probe", "job": make_job()}, {"source_text": "synthetic"}):
        with pytest.raises(ValidationError):
            BrokerOperation.model_validate(
                {
                    "operation": "probe",
                    "broker_url": SYNTHETIC_BROKER_URL,
                    "runtime_location": "host",
                    "deadline": 1,
                }
                | changes
            )


def test_repair_tick_uses_same_private_pipe_without_caller_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor, process = install_process_mock(monkeypatch)
    make_publisher().publish_repair_tick(timeout_seconds=2)
    payload = json.loads(process.communicate.call_args_list[0].kwargs["input"])
    assert payload["operation"] == "repair_tick"
    assert payload["job"] is None
    assert set(payload) == {"operation", "broker_url", "runtime_location", "deadline", "job"}
    assert SYNTHETIC_BROKER_URL not in repr(constructor.call_args)


def test_transport_creates_only_fixed_expiring_repair_signal_without_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = Mock()
    application.__enter__ = Mock(return_value=application)
    application.__exit__ = Mock(return_value=False)
    connection = Mock()
    application.connection_for_write.return_value.__enter__ = Mock(return_value=connection)
    application.connection_for_write.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(outbox_transport, "create_celery_app", Mock(return_value=application))
    request = BrokerOperation(
        operation="repair_tick",
        broker_url=SecretStr(SYNTHETIC_BROKER_URL),
        runtime_location="host",
        deadline=monotonic() + 2,
    )
    assert outbox_transport.execute_operation(request) == 0
    application.send_task.assert_called_once_with(
        REPAIR_OUTBOX_TASK_NAME,
        kwargs={"payload": {"schema_version": "outbox-repair-tick.v1"}},
        queue="document-processing",
        serializer="json",
        ignore_result=True,
        add_to_parent=False,
        connection=connection,
        retry=False,
        retry_policy={"max_retries": 0},
        expires=30,
    )
    with pytest.raises(ValidationError):
        BrokerOperation.model_validate({**request.model_dump(), "job": make_job()})


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (AuthenticationError("synthetic-private-detail"), 2),
        (OSError("synthetic-private-detail"), 1),
        (ValueError("synthetic-private-detail"), 2),
        (TypeError("synthetic-private-detail"), 3),
    ],
)
def test_helper_main_returns_only_safe_categories(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
    expected: int,
) -> None:
    raw = json.dumps(
        {
            "operation": "probe",
            "broker_url": SYNTHETIC_BROKER_URL,
            "runtime_location": "host",
            "deadline": monotonic() + 2,
        }
    ).encode()
    monkeypatch.setattr(sys, "stdin", Mock(buffer=BytesIO(raw)))
    monkeypatch.setattr(outbox_transport, "execute_operation", Mock(side_effect=failure))
    assert outbox_transport.main() == expected
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
