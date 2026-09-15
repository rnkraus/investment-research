"""Unit tests for isolated SeaweedFS verification orchestration."""

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import UUID

import pytest

from scripts import verify_object_storage

PROJECT_UUID = UUID("12345678-1234-4000-8000-123456789abc")
TEST_POSTGRES_PASSWORD = "disposable-postgres-password"
TEST_REDIS_PASSWORD = "disposable-redis-password"
TEST_STORAGE_CREDENTIALS = tuple(f"disposable-storage-credential-{index}" for index in range(4))


def prepare_deterministic_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace discovery and random values without executing external commands."""

    credentials = iter((TEST_POSTGRES_PASSWORD, TEST_REDIS_PASSWORD, *TEST_STORAGE_CREDENTIALS))
    monkeypatch.setattr(
        verify_object_storage,
        "find_docker_executable",
        lambda: "/usr/local/bin/docker",
    )
    monkeypatch.setattr(verify_object_storage, "uuid4", lambda: PROJECT_UUID)
    monkeypatch.setattr(verify_object_storage, "find_available_loopback_port", lambda: 55432)
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
    api_contracts: list[tuple[str, str, Mapping[str, str]]] = []
    secret_paths: list[Path] = []

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
        if label == "Build object-storage verification image":
            path = Path(environment["POSTGRES_PASSWORD_SECRET_FILE"])
            assert path.read_text(encoding="utf-8") == TEST_POSTGRES_PASSWORD
            secret_paths.append(path)
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

    def record_api_contract(
        *,
        image: str,
        project_name: str,
        environment: Mapping[str, str],
    ) -> None:
        api_contracts.append((image, project_name, environment))

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
    monkeypatch.setattr(
        verify_object_storage,
        "run_api_contract_test",
        record_api_contract,
    )

    result = verify_object_storage.verify_object_storage()

    assert result == 0
    assert [label for label, _, _ in calls] == [
        "Build object-storage verification image",
        "Build and start isolated PostgreSQL, Redis, and SeaweedFS",
        "Migrate isolated PostgreSQL database",
        "Seed isolated PostgreSQL workspaces",
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
        "postgres",
        "redis",
        *verify_object_storage.STORAGE_SERVICES,
        *verify_object_storage.STORAGE_SERVICES,
    ]
    assert metadata_credentials == [
        (TEST_POSTGRES_PASSWORD, TEST_REDIS_PASSWORD, *TEST_STORAGE_CREDENTIALS)
    ]
    assert isolated_projects == ["investment-research-storage-check-12345678"]
    assert len(api_contracts) == 1
    assert api_contracts[0][0] == "investment-research-object-storage-verification:12345678"
    assert api_contracts[0][1] == "investment-research-storage-check-12345678"
    assert api_contracts[0][2]["INVESTMENT_RESEARCH_RUNTIME_LOCATION"] == "container"
    assert api_contracts[0][2]["INVESTMENT_RESEARCH_OBJECT_STORAGE_PROVIDER"] == "s3"
    assert api_contracts[0][2]["INVESTMENT_RESEARCH_REDIS_URL"] == (
        f"redis://:{TEST_REDIS_PASSWORD}@redis:6379/0"
    )
    assert calls[-2][1][-3:] == ("down", "--volumes", "--remove-orphans")
    assert calls[-1][1][-2:] == (
        "--force",
        "investment-research-object-storage-verification:12345678",
    )
    assert all(
        credential not in argument
        for _, command, _ in calls
        for argument in command
        for credential in (
            TEST_POSTGRES_PASSWORD,
            TEST_REDIS_PASSWORD,
            *TEST_STORAGE_CREDENTIALS,
        )
    )
    assert calls[0][2]["COMPOSE_DISABLE_ENV_FILE"] == "1"
    assert "POSTGRES_PASSWORD" not in calls[0][2]
    assert "REDIS_PASSWORD" not in calls[0][2]
    assert calls[0][2]["POSTGRES_PORT"] == "55432"
    assert calls[2][2]["INVESTMENT_RESEARCH_DATABASE_URL"].endswith(
        "@127.0.0.1:55432/investment_research"
    )
    direct_s3_command = next(
        command for label, command, _ in calls if label == "Run SeaweedFS S3 contract tests"
    )
    assert "INVESTMENT_RESEARCH_DATABASE_URL" not in direct_s3_command
    assert secret_paths and all(not path.exists() for path in secret_paths)


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
    monkeypatch.setattr(
        verify_object_storage,
        "run_api_contract_test",
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


def test_api_contract_container_uses_both_data_networks_and_is_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, tuple[str, ...], bool]] = []
    environment = {
        name: f"value-{index}"
        for index, name in enumerate(verify_object_storage.API_CONTRACT_ENVIRONMENT_NAMES)
    }

    def record_command(
        label: str,
        command: Sequence[str],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del working_directory, environment
        calls.append((label, tuple(command), check))
        return successful_command(command)

    monkeypatch.setattr(verify_object_storage, "run_command", record_command)

    verify_object_storage.run_api_contract_test(
        image="verification-image:test",
        project_name="isolated-project",
        environment=environment,
    )

    assert [label for label, _, _ in calls] == [
        "Create isolated API object-storage contract container",
        "Connect API contract container to isolated PostgreSQL",
        "Run API capture and authorized original-open contract",
        "Remove isolated API object-storage contract container",
    ]
    assert "isolated-project_storage-data" in calls[0][1]
    assert "INVESTMENT_RESEARCH_DATABASE_URL" in calls[0][1]
    assert "INVESTMENT_RESEARCH_REDIS_URL" in calls[0][1]
    assert calls[1][1][-2:] == (
        "isolated-project_backend",
        "isolated-project-api-contract",
    )
    assert calls[2][1][-2:] == ("--attach", "isolated-project-api-contract")
    assert calls[3][1][-1] == "isolated-project-api-contract"
    assert calls[3][2] is False
    assert all(
        value not in argument
        for _, command, _ in calls
        for argument in command
        for value in environment.values()
    )


def test_disposable_credentials_are_distinct_and_safe() -> None:
    first = verify_object_storage.generate_disposable_credential()
    second = verify_object_storage.generate_disposable_credential()

    assert first != second
    assert len(first) == 48
    assert first.isalnum()
