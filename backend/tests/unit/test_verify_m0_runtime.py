"""Unit tests for complete M0 runtime verification orchestration."""

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import UUID

import pytest

from scripts import verify_m0_runtime

RUN_UUID = UUID("12345678-1234-4000-8000-123456789abc")
TEST_CREDENTIAL = "synthetic-disposable-credential"


def prepare_deterministic_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace host discovery and random inputs with deterministic values."""

    monkeypatch.setattr(
        verify_m0_runtime,
        "find_docker_executable",
        lambda: "/usr/local/bin/docker",
    )
    monkeypatch.setattr(verify_m0_runtime, "uuid4", lambda: RUN_UUID)
    available_ports = iter((55432, 58000, 53000))
    monkeypatch.setattr(
        verify_m0_runtime,
        "find_available_loopback_port",
        lambda: next(available_ports),
    )
    monkeypatch.setattr(
        verify_m0_runtime,
        "generate_disposable_credential",
        lambda: TEST_CREDENTIAL,
    )


def test_verification_runs_complete_journey_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_deterministic_runtime(monkeypatch)
    calls: list[tuple[str, tuple[str, ...], Mapping[str, str]]] = []
    secret_paths: list[Path] = []

    def record_command(
        label: str,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        working_directory: Path = verify_m0_runtime.REPOSITORY_ROOT,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        del working_directory, check
        calls.append((label, tuple(command), environment))
        if label == "Build and start isolated complete M0 runtime":
            secret_path = Path(environment["POSTGRES_PASSWORD_SECRET_FILE"])
            assert secret_path.read_text(encoding="utf-8") == TEST_CREDENTIAL
            secret_paths.append(secret_path)
        output = b"safe synthetic runtime log" if capture_output else None
        return subprocess.CompletedProcess(command, 0, stdout=output)

    monkeypatch.setattr(verify_m0_runtime, "run_command", record_command)

    assert verify_m0_runtime.verify_m0_runtime() == 0
    assert [label for label, _, _ in calls] == [
        "Build and start isolated complete M0 runtime",
        "Seed isolated PostgreSQL through the internal tool container",
        "Build isolated M0 acceptance image",
        "Run automatic M0 fixture journey",
        "Capture isolated runtime logs for sensitive-content verification",
        "Remove isolated M0 runtime and its volumes",
        "Remove isolated M0 acceptance image",
    ]
    assert all(TEST_CREDENTIAL not in argument for _, command, _ in calls for argument in command)
    assert all("POSTGRES_PASSWORD" not in environment for _, _, environment in calls)
    start_command = calls[0][1]
    assert start_command[-2:] == ("web", "worker")
    assert calls[0][2]["POSTGRES_PORT"] == "55432"
    assert calls[0][2]["INVESTMENT_RESEARCH_API_PORT"] == "58000"
    assert calls[0][2]["INVESTMENT_RESEARCH_WEB_PORT"] == "53000"
    assert calls[1][1][-7:] == (
        "--profile",
        "tools",
        "run",
        "--build",
        "--rm",
        "--no-deps",
        "seed",
    )
    assert calls[-2][1][-3:] == ("down", "--volumes", "--remove-orphans")
    assert calls[-1][1][-3:-1] == ("rm", "--force")
    acceptance_command = calls[3][1]
    assert str(verify_m0_runtime.COMPOSE_TEST_FILE) in acceptance_command
    assert verify_m0_runtime.ACCEPTANCE_TEST in acceptance_command
    assert secret_paths and all(not path.exists() for path in secret_paths)


def test_remove_acceptance_image_accepts_already_absent_image(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def report_missing_image(
        label: str,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        working_directory: Path = verify_m0_runtime.REPOSITORY_ROOT,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        del label, environment, working_directory, check
        assert capture_output is True
        return subprocess.CompletedProcess(
            command,
            1,
            stdout=b"Error response from daemon: No such image: synthetic:test\n",
        )

    monkeypatch.setattr(verify_m0_runtime, "run_command", report_missing_image)

    assert verify_m0_runtime.remove_acceptance_image("synthetic:test", {}) == 0
    assert "already absent" in capsys.readouterr().out


def test_remove_acceptance_image_preserves_unexpected_docker_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def report_docker_failure(
        label: str,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        working_directory: Path = verify_m0_runtime.REPOSITORY_ROOT,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        del label, environment, working_directory, check
        assert capture_output is True
        return subprocess.CompletedProcess(command, 7, stdout=b"synthetic Docker failure\n")

    monkeypatch.setattr(verify_m0_runtime, "run_command", report_docker_failure)

    assert verify_m0_runtime.remove_acceptance_image("synthetic:test", {}) == 7
    assert "Could not remove" in capsys.readouterr().err


def test_verification_reports_failure_and_still_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_deterministic_runtime(monkeypatch)
    labels: list[str] = []

    def fail_journey(
        label: str,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        working_directory: Path = verify_m0_runtime.REPOSITORY_ROOT,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        del working_directory, environment, check
        labels.append(label)
        if label == "Run automatic M0 fixture journey":
            raise subprocess.CalledProcessError(9, command)
        output = b"safe failure diagnostics" if capture_output else None
        return subprocess.CompletedProcess(command, 0, stdout=output)

    monkeypatch.setattr(verify_m0_runtime, "run_command", fail_journey)

    assert verify_m0_runtime.verify_m0_runtime() == 9
    assert labels[-3:] == [
        "Capture isolated runtime logs for sensitive-content verification",
        "Remove isolated M0 runtime and its volumes",
        "Remove isolated M0 acceptance image",
    ]


def test_sensitive_runtime_logs_fail_closed_redact_and_clean_up(
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
        working_directory: Path = verify_m0_runtime.REPOSITORY_ROOT,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        del environment, working_directory, check
        labels.append(label)
        output = f"unsafe log: {TEST_CREDENTIAL}".encode() if capture_output else None
        return subprocess.CompletedProcess(command, 0, stdout=output)

    monkeypatch.setattr(verify_m0_runtime, "run_command", leak_credential)

    assert verify_m0_runtime.verify_m0_runtime() == 1
    captured = capsys.readouterr()
    assert TEST_CREDENTIAL not in captured.out
    assert TEST_CREDENTIAL not in captured.err
    assert "[REDACTED]" in captured.err
    assert labels[-2:] == [
        "Remove isolated M0 runtime and its volumes",
        "Remove isolated M0 acceptance image",
    ]
