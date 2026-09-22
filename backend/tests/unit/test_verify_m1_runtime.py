"""Unit tests for complete M1 runtime verification orchestration."""

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import UUID

import pytest

from scripts import verify_m1_runtime
from scripts.docker_secrets import COMPOSE_SECRET_FILES

RUN_UUID = UUID("87654321-4321-4000-8000-cba987654321")
TEST_CREDENTIAL = "synthetic-m1-disposable-credential"


def prepare_deterministic_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace host discovery and random inputs with deterministic values."""

    monkeypatch.setattr(
        verify_m1_runtime,
        "find_docker_executable",
        lambda: "/usr/local/bin/docker",
    )
    monkeypatch.setattr(
        verify_m1_runtime,
        "find_pnpm_executable",
        lambda: "/usr/local/bin/pnpm",
    )
    monkeypatch.setattr(verify_m1_runtime, "uuid4", lambda: RUN_UUID)
    available_ports = iter((55433, 58001, 53001))
    monkeypatch.setattr(
        verify_m1_runtime,
        "find_available_loopback_port",
        lambda: next(available_ports),
    )
    monkeypatch.setattr(
        verify_m1_runtime,
        "generate_disposable_credential",
        lambda: TEST_CREDENTIAL,
    )


def test_verification_runs_m1_journey_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_deterministic_runtime(monkeypatch)
    calls: list[tuple[str, tuple[str, ...], Mapping[str, str]]] = []

    def record_command(
        label: str,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        working_directory: Path = verify_m1_runtime.REPOSITORY_ROOT,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        del working_directory, check
        calls.append((label, tuple(command), environment))
        output = b"safe synthetic M1 runtime log" if capture_output else None
        return subprocess.CompletedProcess(command, 0, stdout=output)

    monkeypatch.setattr(verify_m1_runtime, "run_command", record_command)

    assert verify_m1_runtime.verify_m1_runtime() == 0
    assert [label for label, _, _ in calls] == [
        "Build and start isolated complete M1 runtime",
        "Seed isolated M1 PostgreSQL through the internal tool container",
        "Build isolated M1 acceptance image",
        "Run automatic M1 PDF and text journey",
        "Run visible M1 Chromium journey",
        "Capture isolated M1 runtime logs for sensitive-content verification",
        "Remove isolated M1 runtime and its volumes",
        "Remove isolated M1 acceptance image",
    ]
    start_command = calls[0][1]
    assert start_command[-2:] == ("web", "worker")
    assert str(verify_m1_runtime.COMPOSE_FILE) in start_command
    assert str(verify_m1_runtime.COMPOSE_TEST_FILE) in start_command
    assert str(verify_m1_runtime.COMPOSE_M1_FILE) in start_command
    assert calls[0][2]["POSTGRES_PORT"] == "55433"
    assert calls[0][2]["INVESTMENT_RESEARCH_API_PORT"] == "58001"
    assert calls[0][2]["INVESTMENT_RESEARCH_WEB_PORT"] == "53001"
    assert calls[0][2]["M0_ACCEPTANCE_FIXTURE_PATH"] == str(verify_m1_runtime.FIXTURE_PDF)
    assert calls[0][2]["M1_ACCEPTANCE_TEXT_FIXTURE_PATH"] == str(verify_m1_runtime.FIXTURE_TEXT)
    acceptance_command = calls[3][1]
    assert verify_m1_runtime.ACCEPTANCE_TEST in acceptance_command
    browser_command = calls[4][1]
    assert browser_command == (
        "/usr/local/bin/pnpm",
        "--filter",
        "@investment-research/frontend",
        "test:browser",
    )
    assert calls[4][2]["PLAYWRIGHT_BASE_URL"] == "http://127.0.0.1:53001"
    secret_source_names = {source_name for source_name, _ in COMPOSE_SECRET_FILES.values()}
    assert secret_source_names.isdisjoint(calls[4][2])
    assert calls[-2][1][-3:] == ("down", "--volumes", "--remove-orphans")
    assert calls[-1][1][-4:] == (
        "docker",
        "image",
        "rm",
        "investment-research-m1-acceptance:87654321",
    )
    assert "--force" not in calls[-1][1]
    assert all(TEST_CREDENTIAL not in argument for _, command, _ in calls for argument in command)
    assert all("POSTGRES_PASSWORD" not in environment for _, _, environment in calls)


def test_verification_preserves_failure_and_still_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_deterministic_runtime(monkeypatch)
    labels: list[str] = []

    def fail_journey(
        label: str,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        working_directory: Path = verify_m1_runtime.REPOSITORY_ROOT,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        del environment, working_directory, check
        labels.append(label)
        if label == "Run automatic M1 PDF and text journey":
            raise subprocess.CalledProcessError(9, command)
        output = b"safe failure diagnostics" if capture_output else None
        return subprocess.CompletedProcess(command, 0, stdout=output)

    monkeypatch.setattr(verify_m1_runtime, "run_command", fail_journey)

    assert verify_m1_runtime.verify_m1_runtime() == 9
    assert labels[-3:] == [
        "Capture isolated M1 runtime logs for sensitive-content verification",
        "Remove isolated M1 runtime and its volumes",
        "Remove isolated M1 acceptance image",
    ]


def test_sensitive_runtime_logs_are_redacted_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepare_deterministic_runtime(monkeypatch)
    labels: list[str] = []

    def leak_credential(
        label: str,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        working_directory: Path = verify_m1_runtime.REPOSITORY_ROOT,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        del environment, working_directory, check
        labels.append(label)
        output = f"unsafe log: {TEST_CREDENTIAL}".encode() if capture_output else None
        return subprocess.CompletedProcess(command, 0, stdout=output)

    monkeypatch.setattr(verify_m1_runtime, "run_command", leak_credential)

    assert verify_m1_runtime.verify_m1_runtime() == 1
    captured = capsys.readouterr()
    assert TEST_CREDENTIAL not in captured.out
    assert TEST_CREDENTIAL not in captured.err
    assert "[REDACTED]" in captured.err
    assert labels[-2:] == [
        "Remove isolated M1 runtime and its volumes",
        "Remove isolated M1 acceptance image",
    ]
