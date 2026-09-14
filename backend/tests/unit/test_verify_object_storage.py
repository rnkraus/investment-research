"""Unit tests for isolated SeaweedFS verification orchestration."""

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import UUID

import pytest

from scripts import verify_object_storage

PROJECT_UUID = UUID("12345678-1234-4000-8000-123456789abc")
TEST_CREDENTIALS = tuple(f"disposable-credential-{index}" for index in range(5))


def prepare_deterministic_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace discovery and random values without executing external commands."""

    credentials = iter(TEST_CREDENTIALS)
    monkeypatch.setattr(
        verify_object_storage,
        "find_docker_executable",
        lambda: "/usr/local/bin/docker",
    )
    monkeypatch.setattr(verify_object_storage, "uuid4", lambda: PROJECT_UUID)
    monkeypatch.setattr(
        verify_object_storage,
        "generate_disposable_credential",
        lambda: next(credentials),
    )


def successful_command(
    command: Sequence[str],
) -> subprocess.CompletedProcess[str]:
    """Return one typed successful subprocess result."""

    return subprocess.CompletedProcess(command, 0)


def test_verification_runs_complete_sequence_without_exposing_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_deterministic_runtime(monkeypatch)
    calls: list[tuple[str, tuple[str, ...], Mapping[str, str]]] = []
    healthy_services: list[str] = []
    metadata_credentials: list[tuple[str, ...]] = []
    isolated_projects: list[str] = []

    def record_command(
        label: str,
        command: Sequence[str],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del working_directory, check
        calls.append((label, tuple(command), environment))
        return successful_command(command)

    def record_healthy_service(
        compose_command: Sequence[str],
        service: str,
        *,
        environment: Mapping[str, str],
        timeout_seconds: float = verify_object_storage.HEALTH_TIMEOUT_SECONDS,
    ) -> None:
        del compose_command, environment, timeout_seconds
        healthy_services.append(service)

    def record_metadata(
        compose_command: Sequence[str],
        *,
        environment: Mapping[str, str],
        credentials: Sequence[str],
    ) -> None:
        del compose_command, environment
        metadata_credentials.append(tuple(credentials))

    def record_network_isolation(
        project_name: str,
        *,
        environment: Mapping[str, str],
    ) -> None:
        del environment
        isolated_projects.append(project_name)

    monkeypatch.setattr(verify_object_storage, "run_command", record_command)
    monkeypatch.setattr(
        verify_object_storage,
        "wait_for_healthy_service",
        record_healthy_service,
    )
    monkeypatch.setattr(
        verify_object_storage,
        "wait_for_successful_bootstrap",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(verify_object_storage, "verify_runtime_metadata", record_metadata)
    monkeypatch.setattr(
        verify_object_storage,
        "verify_data_network_isolation",
        record_network_isolation,
    )

    result = verify_object_storage.verify_object_storage()

    assert result == 0
    assert [label for label, _, _ in calls] == [
        "Build object-storage verification image",
        "Build and start isolated SeaweedFS",
        "Verify idempotent bucket bootstrap",
        "Run SeaweedFS S3 contract tests",
        "Write restart-persistence probe object",
        "Stop isolated SeaweedFS data path",
        "Restart seaweedfs-master",
        "Restart seaweedfs-volume",
        "Restart seaweedfs-filer",
        "Restart seaweedfs-s3",
        "Read restart-persistence probe object",
        "Remove isolated SeaweedFS project and its volumes",
        "Remove isolated object-storage verification image",
    ]
    assert healthy_services == [
        *verify_object_storage.STORAGE_SERVICES,
        *verify_object_storage.STORAGE_SERVICES,
    ]
    assert metadata_credentials == [TEST_CREDENTIALS[:4]]
    assert isolated_projects == ["investment-research-storage-check-12345678"]
    assert calls[-2][1][-3:] == ("down", "--volumes", "--remove-orphans")
    assert calls[-1][1][-2:] == (
        "--force",
        "investment-research-object-storage-verification:12345678",
    )
    assert all(
        credential not in argument
        for _, command, _ in calls
        for argument in command
        for credential in TEST_CREDENTIALS
    )
    assert calls[0][2]["COMPOSE_DISABLE_ENV_FILE"] == "1"
    assert calls[0][2]["POSTGRES_PORT"] == "55432"


def test_verification_cleans_up_and_preserves_failure_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_deterministic_runtime(monkeypatch)
    labels: list[str] = []

    def fail_during_contract_tests(
        label: str,
        command: Sequence[str],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del working_directory, environment, check
        labels.append(label)
        if label == "Run SeaweedFS S3 contract tests":
            raise subprocess.CalledProcessError(7, command)
        return successful_command(command)

    monkeypatch.setattr(verify_object_storage, "run_command", fail_during_contract_tests)
    monkeypatch.setattr(
        verify_object_storage,
        "wait_for_healthy_service",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        verify_object_storage,
        "wait_for_successful_bootstrap",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        verify_object_storage,
        "verify_runtime_metadata",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        verify_object_storage,
        "verify_data_network_isolation",
        lambda *args, **kwargs: None,
    )

    result = verify_object_storage.verify_object_storage()

    assert result == 7
    assert labels[-2:] == [
        "Remove isolated SeaweedFS project and its volumes",
        "Remove isolated object-storage verification image",
    ]
    assert "Write restart-persistence probe object" not in labels


def test_missing_docker_stops_before_generating_runtime_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify_object_storage, "find_docker_executable", lambda: None)

    result = verify_object_storage.verify_object_storage()

    assert result == 127


def test_disposable_credentials_are_distinct_and_safe() -> None:
    first = verify_object_storage.generate_disposable_credential()
    second = verify_object_storage.generate_disposable_credential()

    assert first != second
    assert len(first) == 48
    assert first.isalnum()
