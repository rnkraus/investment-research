"""Unit tests for backend container verification orchestration."""

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import UUID

import pytest

from scripts import verify_backend_images

RUN_UUID = UUID("12345678-1234-4000-8000-123456789abc")


def image_configuration(target: str) -> dict[str, object]:
    """Return safe Docker metadata matching one expected target."""

    expectation = verify_backend_images.TARGETS[target]
    configuration: dict[str, object] = {
        "User": verify_backend_images.EXPECTED_USER,
        "Entrypoint": list(expectation.entrypoint),
        "Cmd": list(expectation.command),
        "Env": ["PYTHONUNBUFFERED=1"],
    }
    if expectation.has_healthcheck:
        configuration["Healthcheck"] = {"Test": ["CMD", "safe-readiness-check"]}
    return configuration


def target_from_image(image: str) -> str:
    """Resolve the target segment from a deterministic verification tag."""

    return image.removeprefix(f"{verify_backend_images.IMAGE_PREFIX}-").split(":", 1)[0]


def test_image_verification_builds_checks_and_cleans_every_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify_backend_images, "find_docker_executable", lambda: "/usr/bin/docker")
    monkeypatch.setattr(verify_backend_images, "uuid4", lambda: RUN_UUID)
    calls: list[tuple[str, tuple[str, ...]]] = []
    api_checks: list[tuple[str, str]] = []

    def record_command(
        label: str,
        command: Sequence[str],
        *,
        capture_output: bool = False,
        check: bool = True,
        environment: Mapping[str, str] | None = None,
        working_directory: Path = verify_backend_images.REPOSITORY_ROOT,
        visible: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del check, environment, working_directory, visible
        calls.append((label, tuple(command)))
        stdout = ""
        if label == "Inspect image configuration":
            target = target_from_image(command[-1])
            stdout = json.dumps([{"Config": image_configuration(target)}])
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(verify_backend_images, "run_command", record_command)
    monkeypatch.setattr(
        verify_backend_images,
        "verify_api_against_migrated_postgres",
        lambda image, run_identifier: api_checks.append((image, run_identifier)),
    )

    assert verify_backend_images.verify_backend_images() == 0
    build_commands = [command for label, command in calls if label.startswith("Build backend")]
    assert [command[command.index("--target") + 1] for command in build_commands] == [
        "api",
        "worker",
        "parser",
    ]
    assert len([label for label, _ in calls if label == "Verify minimal non-root runtime"]) == 3
    assert api_checks == [("investment-research-backend-check-api:12345678", "12345678")]
    cleanup_images = [
        command[-1] for label, command in calls if label == "Remove verification image"
    ]
    assert [target_from_image(image) for image in cleanup_images] == ["parser", "worker", "api"]


def test_image_verification_cleans_up_after_an_invariant_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify_backend_images, "find_docker_executable", lambda: "/usr/bin/docker")
    monkeypatch.setattr(verify_backend_images, "uuid4", lambda: RUN_UUID)
    labels: list[str] = []

    def return_root_image(
        label: str,
        command: Sequence[str],
        *,
        capture_output: bool = False,
        check: bool = True,
        environment: Mapping[str, str] | None = None,
        working_directory: Path = verify_backend_images.REPOSITORY_ROOT,
        visible: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del capture_output, check, environment, working_directory, visible
        labels.append(label)
        stdout = json.dumps([{"Config": image_configuration("api") | {"User": "0:0"}}])
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(verify_backend_images, "run_command", return_root_image)

    assert verify_backend_images.verify_backend_images() == 1
    assert labels == [
        "Build backend api target",
        "Inspect image configuration",
        "Inspect image history",
        "Remove verification image",
    ]


def test_image_verification_rejects_secret_names_in_metadata() -> None:
    expectation = verify_backend_images.TARGETS["api"]

    with pytest.raises(
        verify_backend_images.ImageVerificationError,
        match="forbidden configuration name",
    ):
        verify_backend_images.verify_configuration(
            image_configuration("api"),
            "RUN export POSTGRES_PASSWORD=synthetic-marker",
            expectation,
        )


def test_api_runtime_verification_migrates_checks_and_cleans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, tuple[str, ...], Mapping[str, str] | None]] = []
    monkeypatch.setattr(verify_backend_images, "find_available_loopback_port", lambda: 55432)
    monkeypatch.setattr(
        verify_backend_images,
        "generate_disposable_password",
        lambda: "disposable-password",
    )

    def record_command(
        label: str,
        command: Sequence[str],
        *,
        capture_output: bool = False,
        check: bool = True,
        environment: Mapping[str, str] | None = None,
        working_directory: Path = verify_backend_images.REPOSITORY_ROOT,
        visible: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del capture_output, check, working_directory, visible
        calls.append((label, tuple(command), environment))
        stdout = "healthy\n" if label == "Inspect API container health" else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(verify_backend_images, "run_command", record_command)

    verify_backend_images.verify_api_against_migrated_postgres(
        "investment-research-backend-check-api:test",
        "12345678",
    )

    labels = [label for label, _, _ in calls]
    assert labels == [
        "Start isolated PostgreSQL for the API image",
        "Migrate the isolated database to Alembic head",
        "Start the API image with runtime-only configuration",
        "Inspect API container health",
        "Verify layered API health endpoints",
        "Remove the API verification container",
        "Remove isolated PostgreSQL, network, and volume",
    ]
    api_command = next(
        command
        for label, command, _ in calls
        if label == "Start the API image with runtime-only configuration"
    )
    assert "disposable-password" not in api_command
    assert not any("postgresql+psycopg" in argument for argument in api_command)
    assert api_command.count("--env") == 5

    migration_environment = next(
        environment
        for label, _, environment in calls
        if label == "Migrate the isolated database to Alembic head"
    )
    assert migration_environment is not None
    assert migration_environment["INVESTMENT_RESEARCH_DATABASE_URL"].endswith(
        "@127.0.0.1:55432/investment_research"
    )
    assert migration_environment["COMPOSE_DISABLE_ENV_FILE"] == "1"
    assert migration_environment["INVESTMENT_RESEARCH_S3_BUCKET"] == "unused-storage-check"
    assert "unused-application-access" not in api_command
    assert "unused-application-secret" not in api_command


def test_api_runtime_verification_removes_postgres_after_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    labels: list[str] = []
    monkeypatch.setattr(verify_backend_images, "find_available_loopback_port", lambda: 55432)
    monkeypatch.setattr(
        verify_backend_images,
        "generate_disposable_password",
        lambda: "disposable-password",
    )

    def fail_api_start(
        label: str,
        command: Sequence[str],
        *,
        capture_output: bool = False,
        check: bool = True,
        environment: Mapping[str, str] | None = None,
        working_directory: Path = verify_backend_images.REPOSITORY_ROOT,
        visible: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del capture_output, check, environment, working_directory, visible
        labels.append(label)
        if label == "Start the API image with runtime-only configuration":
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(verify_backend_images, "run_command", fail_api_start)

    with pytest.raises(subprocess.CalledProcessError):
        verify_backend_images.verify_api_against_migrated_postgres(
            "investment-research-backend-check-api:test",
            "12345678",
        )

    assert "Remove the API verification container" not in labels
    assert labels[-1] == "Remove isolated PostgreSQL, network, and volume"
