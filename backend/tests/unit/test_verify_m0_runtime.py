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
    monkeypatch.setattr(verify_m0_runtime, "find_available_loopback_port", lambda: 55432)
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
    ) -> subprocess.CompletedProcess[bytes]:
        del working_directory, check
        calls.append((label, tuple(command), environment))
        if label == "Build and start isolated M0 processing runtime":
            secret_path = Path(environment["POSTGRES_PASSWORD_SECRET_FILE"])
            assert secret_path.read_text(encoding="utf-8") == TEST_CREDENTIAL
            secret_paths.append(secret_path)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(verify_m0_runtime, "run_command", record_command)

    assert verify_m0_runtime.verify_m0_runtime() == 0
    assert [label for label, _, _ in calls] == [
        "Build and start isolated M0 processing runtime",
        "Migrate isolated PostgreSQL database",
        "Seed isolated PostgreSQL workspaces",
        "Build isolated M0 acceptance image",
        "Run automatic M0 fixture journey",
        "Remove isolated M0 runtime and its volumes",
        "Remove isolated M0 acceptance image",
    ]
    assert all(TEST_CREDENTIAL not in argument for _, command, _ in calls for argument in command)
    assert all("POSTGRES_PASSWORD" not in environment for _, _, environment in calls)
    assert calls[-2][1][-3:] == ("down", "--volumes", "--remove-orphans")
    assert calls[-1][1][-3:-1] == ("rm", "--force")
    acceptance_command = calls[4][1]
    assert str(verify_m0_runtime.COMPOSE_TEST_FILE) in acceptance_command
    assert verify_m0_runtime.ACCEPTANCE_TEST in acceptance_command
    assert secret_paths and all(not path.exists() for path in secret_paths)


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
    ) -> subprocess.CompletedProcess[bytes]:
        del working_directory, environment, check
        labels.append(label)
        if label == "Run automatic M0 fixture journey":
            raise subprocess.CalledProcessError(9, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(verify_m0_runtime, "run_command", fail_journey)

    assert verify_m0_runtime.verify_m0_runtime() == 9
    assert labels[-3:] == [
        "Show isolated worker logs after verification failure",
        "Remove isolated M0 runtime and its volumes",
        "Remove isolated M0 acceptance image",
    ]
