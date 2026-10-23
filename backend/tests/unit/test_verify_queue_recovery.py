"""Unit tests for live queue-recovery verification orchestration."""

import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy import Engine

from investment_research.infrastructure.queue.celery import REPAIR_OUTBOX_TASK_NAME
from investment_research.modules.processing.domain import ProcessingState
from scripts import verify_queue_recovery
from scripts.queue_http_recovery_probe import HttpRecoveryVerificationError
from scripts.repair_scheduler_probe import SchedulerVerificationError

RUN_UUID = UUID("12345678-1234-4000-8000-123456789abc")
PROCESSING_RUN_ID = UUID("10000000-0000-4000-8000-000000000004")
DOCUMENT_ID = UUID("10000000-0000-4000-8000-000000000001")
TEST_CREDENTIAL = "synthetic-disposable-credential"


def test_capture_probe_uses_current_pdf_capture_contract() -> None:
    """Keep the code executed inside the worker image aligned with M1 capture names."""

    assert "PdfCaptureInput" in verify_queue_recovery.CAPTURE_PROBE_CODE
    assert ".capture_pdf(" in verify_queue_recovery.CAPTURE_PROBE_CODE
    assert "CaptureDocumentInput" not in verify_queue_recovery.CAPTURE_PROBE_CODE
    assert "BoundedCeleryOutboxPublisher" in verify_queue_recovery.CAPTURE_PROBE_CODE
    assert "processing_dispatcher=DeferredDispatcher()" in verify_queue_recovery.CAPTURE_PROBE_CODE
    compile(verify_queue_recovery.CAPTURE_PROBE_CODE, "synthetic-capture-probe", "exec")
    assert "RepairOutboxService" in verify_queue_recovery.CAPTURE_PROBE_CODE


def test_outbox_probe_preserves_receipts_fences_and_production_grace() -> None:
    code = verify_queue_recovery.CAPTURE_PROBE_CODE
    assert "publisher.publish(job, timeout_seconds=timeout_seconds)" in code
    assert code.index("publisher.publish(job,") < code.index(
        'raise OutboxPublicationUnavailableError("synthetic lost publication acknowledgement")'
    )
    assert "uncertain.published == 0" in code
    assert "events[0].state is OutboxState.RETRY_WAIT" in code
    assert "events[0].attempt_count == 2" in code
    assert "events[1].attempt_count == 1" in code
    assert "events[0] == published" in code
    assert "premature.unstarted.prepared == premature.delivery.published == 0" in code
    assert "repeated.used_targets == repeated.delivery.published == 0" in code
    assert "clock=lambda: repair_time" in code
    assert "OUTBOX_UNSTARTED_GRACE_SECONDS + 1" in code
    assert "maximum_processing_attempts=maximum_attempts" in code
    assert code.count("lose_one_message()\n") == 2
    assert "broker.lpop(PROCESSING_QUEUE_NAME)" in code
    assert "flushall" not in code.lower() and "flushdb" not in code.lower()
    assert "update(" not in code and "time.sleep" not in code
    assert "run == initial_run" in code


def test_outage_probe_requires_pending_intent_without_consuming_attempts() -> None:
    code = verify_queue_recovery.CAPTURE_PROBE_CODE
    assert 'phase == "capture-outage"' in code
    assert "initial_events[0].state is OutboxState.PENDING" in code
    assert "initial_events[0].attempt_count == 0" in code
    assert "for _ in range(2):" in code
    assert "unavailable.delivery.stop_reason is DrainStopReason.BROKER_UNAVAILABLE" in code
    assert "unavailable.used_targets == unavailable.delivery.claimed == 0" in code
    assert "snapshot() == (initial_run, initial_events)" in code
    assert code.count("capture_service.capture_pdf(") == 1
    assert "ProcessingRunRow.state == ProcessingState.CAPTURED" in code


def prepare_deterministic_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Mock, list[ProcessingState], list[str]]:
    """Replace Docker and database effects with observable deterministic fakes."""

    engine = Mock(spec=Engine)
    states: list[ProcessingState] = []
    events: list[str] = []
    monkeypatch.setattr(
        verify_queue_recovery,
        "find_docker_executable",
        lambda: "/usr/local/bin/docker",
    )
    monkeypatch.setattr(verify_queue_recovery, "uuid4", lambda: RUN_UUID)
    monkeypatch.setattr(verify_queue_recovery, "find_available_loopback_port", lambda: 55432)
    monkeypatch.setattr(
        verify_queue_recovery,
        "generate_disposable_credential",
        lambda: TEST_CREDENTIAL,
    )
    monkeypatch.setattr(
        verify_queue_recovery,
        "create_verification_engine",
        lambda **_: engine,
    )
    monkeypatch.setattr(
        verify_queue_recovery,
        "compose_container_id",
        lambda _, service, **__: f"{service}-container",
    )
    monkeypatch.setattr(
        verify_queue_recovery,
        "find_processing_run_id",
        lambda _: PROCESSING_RUN_ID,
    )
    monkeypatch.setattr(
        verify_queue_recovery,
        "wait_for_processing_state",
        lambda _, __, state: states.append(state),
    )
    monkeypatch.setattr(
        verify_queue_recovery,
        "shorten_active_lease",
        lambda *_: events.append("shorten-lease"),
    )
    monkeypatch.setattr(
        verify_queue_recovery,
        "verify_single_recovered_output",
        lambda *_: events.append("verify-output"),
    )
    monkeypatch.setattr(verify_queue_recovery, "find_document_id", lambda *_: DOCUMENT_ID)
    monkeypatch.setattr(
        verify_queue_recovery, "wait_for_document_purge", lambda *_: events.append("verify-purge")
    )
    monkeypatch.setattr(
        verify_queue_recovery,
        "wait_for_repair_tick",
        lambda *_, **__: events.append("verify-repair-tick"),
    )
    monkeypatch.setattr(
        verify_queue_recovery,
        "verify_periodic_scheduler",
        lambda *_, **__: events.append("verify-periodic-scheduler"),
    )
    monkeypatch.setattr(
        verify_queue_recovery,
        "verify_http_capture_recovery",
        lambda *_, **__: events.append("verify-http-recovery"),
    )
    return engine, states, events


def test_verification_runs_worker_loss_sequence_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, states, events = prepare_deterministic_runtime(monkeypatch)
    calls: list[tuple[str, tuple[str, ...], Mapping[str, str]]] = []
    secret_paths: list[Path] = []

    def record_command(
        label: str,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        working_directory: Path = verify_queue_recovery.REPOSITORY_ROOT,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        del working_directory, check
        calls.append((label, tuple(command), environment))
        if label == "Build and start isolated processing runtime":
            secret_path = Path(environment["POSTGRES_PASSWORD_SECRET_FILE"])
            assert secret_path.read_text(encoding="utf-8") == TEST_CREDENTIAL
            secret_paths.append(secret_path)
        return subprocess.CompletedProcess(
            command,
            137 if label == "Kill the active Celery pool worker after its canonical claim" else 0,
        )

    monkeypatch.setattr(verify_queue_recovery, "run_command", record_command)

    assert verify_queue_recovery.verify_queue_recovery() == 0
    assert [label for label, _, _ in calls] == [
        "Build and start isolated processing runtime",
        "Migrate isolated PostgreSQL database",
        "Seed isolated PostgreSQL workspaces",
        "Pause parser before publishing the recovery probe",
        "Pause isolated worker before removing test publications",
        "Stop isolated Redis before fixture capture",
        "Capture fixture while isolated Redis is stopped",
        "Restart isolated Redis before outbox recovery",
        "Recover retained fixture and lost outbox publications",
        "Resume isolated worker after outbox recovery probes",
        "Kill the active Celery pool worker after its canonical claim",
        "Resume parser for the reclaimed delivery",
        "Publish permanent deletion for the isolated synthetic document",
        "Pause worker before expired-execution fixture",
        "Verify live-fence rejection and expired-execution repair",
        "Resume worker for repaired expired execution",
        "Purge the isolated expired-execution fixture",
        "Publish one explicit repair tick after synthetic cleanup",
        "Verify one due scheduler step through the bounded broker sender",
        "Run one manual repair pass after synthetic cleanup",
        "Start isolated M1 API and worker with default repair scheduler",
        "Remove isolated queue-recovery project and its volumes",
    ]
    assert states == [
        ProcessingState.PARSING,
        ProcessingState.LEXICAL_READY,
        ProcessingState.LEXICAL_READY,
    ]
    assert events == [
        "shorten-lease",
        "verify-output",
        "verify-purge",
        "verify-output",
        "verify-purge",
        "verify-repair-tick",
        "verify-purge",
        "verify-purge",
        "verify-purge",
        "verify-periodic-scheduler",
        "verify-purge",
        "verify-purge",
        "verify-http-recovery",
    ]
    commands = {label: command for label, command, _ in calls}
    assert commands["Stop isolated Redis before fixture capture"][-2:] == ("stop", "redis")
    assert commands["Restart isolated Redis before outbox recovery"][-3:] == (
        "start",
        "--wait",
        "redis",
    )
    assert commands["Capture fixture while isolated Redis is stopped"][-1] == "capture-outage"
    assert commands["Run one manual repair pass after synthetic cleanup"] == (
        "docker",
        "exec",
        "worker-container",
        "/app/.venv/bin/python",
        "-m",
        "investment_research.repair_outbox",
    )
    assert commands["Recover retained fixture and lost outbox publications"][-1] == "recover-loss"
    expired_command = commands["Verify live-fence rejection and expired-execution repair"]
    assert expired_command[-1] == "/verification-expired-execution.py"
    assert "INVESTMENT_RESEARCH_QUEUE_RECOVERY_VERIFICATION=1" in expired_command
    assert (
        f"{verify_queue_recovery.EXPIRED_EXECUTION_PROBE}:/verification-expired-execution.py:ro"
        in expired_command
    )
    assert all(TEST_CREDENTIAL not in argument for _, command, _ in calls for argument in command)
    assert calls[-1][1][-3:] == ("down", "--volumes", "--remove-orphans")
    assert str(verify_queue_recovery.COMPOSE_M1_FILE) in calls[-1][1]
    assert str(verify_queue_recovery.COMPOSE_FILE) in calls[0][1]
    assert str(verify_queue_recovery.COMPOSE_TEST_FILE) in calls[0][1]
    assert all("compose.m2-repair.yaml" not in argument for argument in calls[0][1])
    http_label = "Start isolated M1 API and worker with default repair scheduler"
    assert commands[http_label][-2:] == ("api", "worker")
    assert str(verify_queue_recovery.COMPOSE_M1_FILE) in commands[http_label]
    assert str(verify_queue_recovery.COMPOSE_M1_FILE) not in calls[0][1]
    assert calls[0][2]["INVESTMENT_RESEARCH_API_PORT"] == "55432"
    rag_mount = (
        f"{verify_queue_recovery.REPOSITORY_ROOT / 'config' / 'rag.toml'}:/config/rag.toml:ro"
    )
    for label, command, _ in calls:
        if label in {
            "Capture fixture while isolated Redis is stopped",
            "Recover retained fixture and lost outbox publications",
            "Publish permanent deletion for the isolated synthetic document",
            "Verify live-fence rejection and expired-execution repair",
            "Purge the isolated expired-execution fixture",
        }:
            assert rag_mount in command
            assert command[command.index(rag_mount) - 1] == "--volume"
        else:
            assert rag_mount not in command
    assert all("POSTGRES_PASSWORD" not in environment for _, _, environment in calls)
    assert secret_paths and all(not path.exists() for path in secret_paths)
    engine.dispose.assert_called_once_with()


def test_purge_probe_commits_explicit_intent_before_id_only_publication() -> None:
    code = verify_queue_recovery.PURGE_PROBE_CODE
    compile(code, "synthetic-purge-probe", "exec")
    assert "load_api_settings(env_file=None)" in code
    assert "DeleteDocumentsCommand(document_ids=(document_id,))" in code
    assert code.index("service.request(") < code.index("dispatcher.dispatch(job)")
    assert code.count("dispatcher.dispatch(job)") == 2


@pytest.mark.parametrize(
    ("failure_label", "worker_needs_resume", "parser_needs_resume"),
    [
        ("Pause isolated worker before removing test publications", False, True),
        ("Stop isolated Redis before fixture capture", True, True),
        ("Capture fixture while isolated Redis is stopped", True, True),
        ("Restart isolated Redis before outbox recovery", True, True),
        ("Recover retained fixture and lost outbox publications", True, True),
        ("Resume isolated worker after outbox recovery probes", True, True),
        ("Pause worker before expired-execution fixture", False, False),
        ("Verify live-fence rejection and expired-execution repair", True, False),
        ("Resume worker for repaired expired execution", True, False),
        ("Purge the isolated expired-execution fixture", False, False),
        ("Publish one explicit repair tick after synthetic cleanup", False, False),
        ("Verify one due scheduler step through the bounded broker sender", False, False),
        ("Run one manual repair pass after synthetic cleanup", False, False),
        ("Start isolated M1 API and worker with default repair scheduler", False, False),
    ],
)
def test_verification_resumes_paused_services_and_cleans_up_after_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure_label: str,
    worker_needs_resume: bool,
    parser_needs_resume: bool,
) -> None:
    engine, _, _ = prepare_deterministic_runtime(monkeypatch)
    labels: list[str] = []

    def fail_capture(
        label: str,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        working_directory: Path = verify_queue_recovery.REPOSITORY_ROOT,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        del working_directory, environment, check
        labels.append(label)
        if label == failure_label:
            raise subprocess.CalledProcessError(7, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(verify_queue_recovery, "run_command", fail_capture)

    assert verify_queue_recovery.verify_queue_recovery() == 7
    expected_cleanup = ["Show isolated worker logs after verification failure"]
    if worker_needs_resume:
        expected_cleanup.append("Resume isolated worker before cleanup")
    if parser_needs_resume:
        expected_cleanup.append("Resume parser before cleanup")
    expected_cleanup.append("Remove isolated queue-recovery project and its volumes")
    assert labels[-len(expected_cleanup) :] == expected_cleanup
    engine.dispose.assert_called_once_with()


@pytest.mark.parametrize(
    ("function", "error_type"),
    [
        ("verify_periodic_scheduler", SchedulerVerificationError),
        ("verify_http_capture_recovery", HttpRecoveryVerificationError),
    ],
)
def test_scheduler_or_http_recovery_failure_is_reported_and_project_is_removed(
    monkeypatch: pytest.MonkeyPatch,
    function: str,
    error_type: type[Exception],
) -> None:
    engine, _, _ = prepare_deterministic_runtime(monkeypatch)
    command = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(verify_queue_recovery, "run_command", command)
    monkeypatch.setattr(
        verify_queue_recovery,
        function,
        Mock(side_effect=error_type("synthetic verification failure")),
    )
    assert verify_queue_recovery.verify_queue_recovery() == 1
    assert command.call_args.args[0] == "Remove isolated queue-recovery project and its volumes"
    engine.dispose.assert_called_once()


def test_scheduler_probe_changes_only_disposable_schedule_and_requires_real_send() -> None:
    code = verify_queue_recovery.SCHEDULER_PROBE_CODE
    compile(code, "scheduler-probe", "exec")
    assert "load_scheduler_settings()" in code
    assert "TemporaryDirectory" in code
    assert "scheduler.tick() == 0" in code and "scheduler.tick() > 0" in code
    assert code.count("delivery.repair_tick.published") == 2
    assert "scheduler.close()" in code
    assert "worker import" not in code
    assert "update(" not in code


def test_explicit_repair_probe_is_content_free_and_does_not_enable_a_schedule() -> None:
    code = verify_queue_recovery.REPAIR_TICK_PROBE_CODE
    compile(code, "synthetic-repair-tick-probe", "exec")
    assert "OutboxRepairTickV1.create().model_dump" in code
    assert "retry=False" in code
    assert "beat_schedule" not in code and "workspace_id" not in code


def test_repair_tick_completion_requires_the_exact_task_name_and_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = f"{REPAIR_OUTBOX_TASK_NAME}[expected-id] succeeded"
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=marker, stderr=""))
    monkeypatch.setattr(subprocess, "run", run)
    verify_queue_recovery.wait_for_repair_tick(["docker", "compose"], "expected-id", environment={})
    assert run.call_args.kwargs["capture_output"] is True
    assert run.call_args.kwargs["timeout"] == 5


def test_unrelated_task_completion_cannot_confirm_the_repair_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Mock(side_effect=[0.0, 0.0, verify_queue_recovery.STATE_TIMEOUT_SECONDS])
    monkeypatch.setattr(time, "monotonic", clock)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess([], 0, stdout="other[id] succeeded", stderr="")
        ),
    )
    with pytest.raises(
        verify_queue_recovery.QueueRecoveryVerificationError, match="before timeout"
    ):
        verify_queue_recovery.wait_for_repair_tick(
            ["docker", "compose"], "expected-id", environment={}
        )
