"""Unit tests for live queue-recovery verification orchestration."""

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy import Engine

from investment_research.modules.processing.domain import ProcessingState
from scripts import verify_queue_recovery

RUN_UUID = UUID("12345678-1234-4000-8000-123456789abc")
PROCESSING_RUN_ID = UUID("10000000-0000-4000-8000-000000000004")
DOCUMENT_ID = UUID("10000000-0000-4000-8000-000000000001")
TEST_CREDENTIAL = "synthetic-disposable-credential"


def test_capture_probe_uses_current_pdf_capture_contract() -> None:
    """Keep the code executed inside the worker image aligned with M1 capture names."""

    assert "PdfCaptureInput" in verify_queue_recovery.CAPTURE_PROBE_CODE
    assert ".capture_pdf(" in verify_queue_recovery.CAPTURE_PROBE_CODE
    assert "CaptureDocumentInput" not in verify_queue_recovery.CAPTURE_PROBE_CODE


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
        "Capture fixture and publish one real Celery job",
        "Kill the active Celery pool worker after its canonical claim",
        "Resume parser for the reclaimed delivery",
        "Publish permanent deletion for the isolated synthetic document",
        "Remove isolated queue-recovery project and its volumes",
    ]
    assert states == [ProcessingState.PARSING, ProcessingState.LEXICAL_READY]
    assert events == ["shorten-lease", "verify-output", "verify-purge"]
    assert all(TEST_CREDENTIAL not in argument for _, command, _ in calls for argument in command)
    assert calls[-1][1][-3:] == ("down", "--volumes", "--remove-orphans")
    assert str(verify_queue_recovery.COMPOSE_FILE) in calls[0][1]
    assert str(verify_queue_recovery.COMPOSE_TEST_FILE) in calls[0][1]
    rag_mount = (
        f"{verify_queue_recovery.REPOSITORY_ROOT / 'config' / 'rag.toml'}:/config/rag.toml:ro"
    )
    for label, command, _ in calls:
        if label in {
            "Capture fixture and publish one real Celery job",
            "Publish permanent deletion for the isolated synthetic document",
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


def test_verification_resumes_parser_and_cleans_up_after_capture_failure(
    monkeypatch: pytest.MonkeyPatch,
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
        if label == "Capture fixture and publish one real Celery job":
            raise subprocess.CalledProcessError(7, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(verify_queue_recovery, "run_command", fail_capture)

    assert verify_queue_recovery.verify_queue_recovery() == 7
    assert labels[-3:] == [
        "Show isolated worker logs after verification failure",
        "Resume parser before cleanup",
        "Remove isolated queue-recovery project and its volumes",
    ]
    engine.dispose.assert_called_once_with()
